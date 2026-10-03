"""
End-to-end backend tests: real Postgres, real HTTP uploads to an S3-compatible
server, real ffmpeg. Only the two paid external APIs are replaced:
Gnani by replayed responses (formats verified live) and Gemini by a stub.
"""

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.models import Recording
from app.services.gnani import GnaniClient
from app.services.summarizer import NO_SPEECH_SUMMARY, Summarizer, SummaryError
from app.status import Status
from app.worker import run as worker
from app.worker.pipeline import process_recording

SAMPLES = Path(__file__).resolve().parent.parent / "scripts" / "samples"
ME = "client-aaaa-1111"
SOMEONE_ELSE = "client-bbbb-2222"


# ---------- helpers ----------

def upload(api, path: Path, client_id=ME, language="en-IN", filename=None) -> uuid.UUID:
    data = path.read_bytes()
    r = api.post(
        "/api/recordings",
        json={"filename": filename or path.name, "content_type": "audio/wav", "size_bytes": len(data), "language_code": language},
        headers={"X-Client-Id": client_id},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    put = httpx.put(body["upload_url"], content=data, headers=body["upload_headers"], timeout=60)  # straight to "R2"
    assert put.status_code == 200, put.text
    api.uploaded[body["recording"]["id"]] = data
    r = api.post(f"/api/recordings/{body['recording']['id']}/uploaded", headers={"X-Client-Id": client_id})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    return uuid.UUID(body["recording"]["id"])


def fake_gnani(responses):
    """GnaniClient whose HTTP calls return `responses` in order: (status, body) tuples."""
    calls = []
    queue = list(responses)

    def handler(request):
        calls.append(request)
        status, body = queue.pop(0)
        return httpx.Response(status, json=body)

    client = GnaniClient("k", "https://gnani.test/stt/v3", http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    return client, calls


def ok(text):
    return (200, {"success": True, "request_id": f"req-{text}", "transcript": text})


class StubLLM:
    def __init__(self, fail_first=0):
        self.calls, self.fail_first = 0, fail_first

    def summarize(self, transcript, language_code):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise SummaryError("The summary service returned an error. You can retry.", "529 overloaded")
        return f"SUMMARY OF: {transcript[:40]}"


def run_worker_once(db, gnani, llm, concurrency=1):
    job = worker.claim_next_job(db)
    assert job is not None
    process_recording(db, job, gnani=gnani, summarizer=llm, chunk_seconds=25, concurrency=concurrency)
    db.expire_all()
    rec = db.get(Recording, job)
    if rec.status == Status.FAILED:
        print("pipeline failed:", rec.error_message, "|", rec.error_detail)  # shown by pytest on assertion failure
    return rec


# ---------- schema & API ----------

def test_migration_matches_models(database):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.db import Base, engine

    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_health(api):
    assert api.get("/health").json() == {"status": "ok", "database": "ok"}


def test_upload_flow_and_per_browser_scoping(api):
    rec_id = upload(api, SAMPLES / "sample_en.wav")

    mine = api.get("/api/recordings", headers={"X-Client-Id": ME}).json()
    assert [r["id"] for r in mine] == [str(rec_id)]
    assert api.get("/api/recordings", headers={"X-Client-Id": SOMEONE_ELSE}).json() == []  # no global list
    assert api.get("/api/recordings").status_code == 422  # client id required to list

    detail = api.get(f"/api/recordings/{rec_id}").json()  # readable by its unguessable UUID
    assert detail["status"] == "queued" and detail["chunks"] == []
    assert "storage_key" not in detail and "owner_id" not in detail and "error_detail" not in detail

    # someone else can't modify it
    assert api.post(f"/api/recordings/{rec_id}/retry", headers={"X-Client-Id": SOMEONE_ELSE}).status_code == 404

    audio_url = api.get(f"/api/recordings/{rec_id}/audio").json()["url"]
    assert f"uploads/{rec_id}/original.wav" in audio_url and "X-Amz-Signature=" in audio_url

    # The object really landed in the bucket via the browser-style presigned PUT, at the right size.
    from app.services import storage
    assert storage.object_size(f"uploads/{rec_id}/original.wav") == (SAMPLES / "sample_en.wav").stat().st_size


def test_storage_download_roundtrip(s3, tmp_path):
    """The real download_to(), with a file small enough to get past the laptop's antivirus."""
    from app.services import storage

    storage.get_client().put_object(Bucket=storage.bucket(), Key="roundtrip/test.bin", Body=b"audio-bytes" * 50)
    storage.download_to("roundtrip/test.bin", tmp_path / "out.bin")
    assert (tmp_path / "out.bin").read_bytes() == b"audio-bytes" * 50
    assert storage.object_size("roundtrip/missing.bin") is None


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"filename": "notes.pdf", "size_bytes": 10, "language_code": "en-IN"}, "Unsupported file type"),
        ({"filename": "a.mp3", "size_bytes": 10, "language_code": "fr-FR"}, "Unsupported language"),
        ({"filename": "a.mp3", "size_bytes": 10**12, "language_code": "en-IN"}, "too large"),
    ],
)
def test_create_validation(api, payload, expected):
    r = api.post("/api/recordings", json=payload, headers={"X-Client-Id": ME})
    assert r.status_code == 400 and expected in r.json()["detail"]


def test_confirming_an_upload_that_never_arrived(api):
    r = api.post(
        "/api/recordings", json={"filename": "a.wav", "size_bytes": 100, "language_code": "en-IN"}, headers={"X-Client-Id": ME}
    )
    rec_id = r.json()["recording"]["id"]
    r = api.post(f"/api/recordings/{rec_id}/uploaded", headers={"X-Client-Id": ME})
    assert r.status_code == 400 and "not found in storage" in r.json()["detail"]
    assert api.get(f"/api/recordings/{rec_id}").json()["status"] == "uploading"


# ---------- worker / pipeline ----------

def test_long_file_is_chunked_transcribed_and_summarized(api, db):
    rec_id = upload(api, SAMPLES / "long_75s_en.wav")  # 68.9 s -> 3 chunks
    gnani, calls = fake_gnani([ok("one"), ok("two"), ok("three")])
    rec = run_worker_once(db, gnani, StubLLM())

    assert rec.status == Status.COMPLETED
    assert rec.duration_seconds == pytest.approx(68.9, abs=0.2)
    assert (rec.chunks_total, rec.chunks_done, len(calls)) == (3, 3, 3)
    assert [(c.start_seconds, c.status, c.gnani_request_id) for c in rec.chunks] == [
        (0.0, "done", "req-one"), (25.0, "done", "req-two"), (50.0, "done", "req-three")
    ]
    assert rec.transcript == "one two three"  # joined in chunk order
    assert rec.summary == "SUMMARY OF: one two three"
    assert rec.completed_at is not None and rec.error_message is None

    body = api.get(f"/api/recordings/{rec_id}").json()
    assert body["status"] == "completed" and len(body["chunks"]) == 3


def test_parallel_transcription_completes_every_chunk(api, db):
    upload(api, SAMPLES / "long_75s_en.wav")
    gnani, calls = fake_gnani([ok("x"), ok("x"), ok("x")])
    rec = run_worker_once(db, gnani, StubLLM(), concurrency=3)
    assert rec.status == Status.COMPLETED and rec.chunks_done == 3 and len(calls) == 3


def test_failed_chunk_then_retry_only_resends_that_chunk(api, db):
    rec_id = upload(api, SAMPLES / "long_75s_en.wav")
    # chunk 0 ok; chunk 1 gets 500 three times (retries exhausted); chunk 2 ok
    gnani, calls = fake_gnani([ok("one"), (500, {}), (500, {}), (500, {}), ok("three")])
    rec = run_worker_once(db, gnani, StubLLM())

    assert rec.status == Status.FAILED and rec.can_retry
    assert "1 of 3 segments" in rec.error_message
    assert [c.status for c in rec.chunks] == ["done", "failed", "done"]
    assert rec.chunks[1].attempts == 3 and rec.transcript is None

    r = api.post(f"/api/recordings/{rec_id}/retry", headers={"X-Client-Id": ME})
    assert r.status_code == 200 and r.json()["status"] == "queued"

    gnani2, calls2 = fake_gnani([ok("two")])
    rec = run_worker_once(db, gnani2, StubLLM())
    assert len(calls2) == 1  # only the failed chunk was sent again
    assert rec.status == Status.COMPLETED and rec.transcript == "one two three"
    assert len(rec.chunks) == 3  # no duplicate chunk rows


def test_bad_gnani_key_stops_immediately_without_retrying(db, api):
    upload(api, SAMPLES / "long_75s_en.wav")  # 3 chunks
    bad_key = (401, {"detail": {"error_code": "INVALID_API_KEY", "message": "Unauthorized"}})
    gnani, calls = fake_gnani([bad_key] * 3)
    rec = run_worker_once(db, gnani, StubLLM())
    # No retries, and no new chunks after the 401. With one thread, the pool may already have
    # started the next chunk before we saw the 401, so at most 2 of the 3 chunks are sent.
    assert len(calls) <= 2
    assert all(c.attempts <= 1 for c in rec.chunks)
    assert rec.status == Status.FAILED and "credentials" in rec.error_message
    assert "INVALID_API_KEY" in rec.error_detail


def test_summary_failure_keeps_transcript_and_retry_only_resummarizes(api, db):
    rec_id = upload(api, SAMPLES / "sample_en.wav")
    gnani, calls = fake_gnani([ok("hello")])
    llm = StubLLM(fail_first=1)
    rec = run_worker_once(db, gnani, llm)
    assert rec.status == Status.FAILED and rec.transcript == "hello" and rec.summary is None
    assert "summary" in rec.error_message

    api.post(f"/api/recordings/{rec_id}/retry", headers={"X-Client-Id": ME})
    gnani2, calls2 = fake_gnani([])
    rec = run_worker_once(db, gnani2, llm)
    assert calls2 == [] and llm.calls == 2  # no re-transcription, one more LLM call
    assert rec.status == Status.COMPLETED and rec.summary == "SUMMARY OF: hello"


def test_silence_completes_with_no_speech(api, db):
    upload(api, SAMPLES / "silence_10s.wav")
    gnani, _ = fake_gnani([(200, {"success": True, "request_id": "r", "transcript": ""})])
    fake_llm_client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: pytest.fail("LLM must not be called")))
    rec = run_worker_once(db, gnani, Summarizer(client=fake_llm_client, model="m"))
    assert rec.status == Status.COMPLETED and rec.transcript == "" and rec.summary == NO_SPEECH_SUMMARY


def test_corrupt_file_fails_with_clear_message(api, db, tmp_path):
    bad = tmp_path / "broken.mp3"
    bad.write_bytes(b"not audio at all" * 200)
    upload(api, bad)
    gnani, calls = fake_gnani([])
    rec = run_worker_once(db, gnani, StubLLM())
    assert rec.status == Status.FAILED and "corrupted" in rec.error_message and calls == []


def test_storage_download_failure_is_reported_clearly(api, db, monkeypatch):
    from boto3.exceptions import RetriesExceededError

    from app.services import storage

    upload(api, SAMPLES / "sample_en.wav")

    def broken_download(key, path):
        raise RetriesExceededError("Max Retries Exceeded")  # what boto3 raises when downloads keep failing

    monkeypatch.setattr(storage, "download_to", broken_download)
    gnani, calls = fake_gnani([])
    rec = run_worker_once(db, gnani, StubLLM())
    assert rec.status == Status.FAILED and "Could not read the uploaded file" in rec.error_message
    assert rec.can_retry and calls == []


# ---------- queue mechanics ----------

def test_skip_locked_prevents_two_workers_taking_the_same_job(api, db):
    from app.db import SessionLocal

    upload(api, SAMPLES / "sample_en.wav")
    other_worker = SessionLocal()
    locked = other_worker.scalars(select(Recording).with_for_update()).first()  # holds the row lock
    assert locked is not None
    assert worker.claim_next_job(db) is None  # locked row is skipped, not waited on
    other_worker.rollback()
    other_worker.close()
    assert worker.claim_next_job(db) is not None


def test_sweeper_requeues_dead_jobs_and_expires_abandoned_uploads(api, db):
    rec_id = upload(api, SAMPLES / "sample_en.wav")
    worker.claim_next_job(db)
    rec = db.get(Recording, rec_id)
    rec.locked_at = datetime.now(timezone.utc) - timedelta(minutes=30)  # worker "died" long ago

    r = api.post("/api/recordings", json={"filename": "a.wav", "size_bytes": 5, "language_code": "en-IN"}, headers={"X-Client-Id": ME})
    abandoned = db.get(Recording, uuid.UUID(r.json()["recording"]["id"]))
    abandoned.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.commit()

    worker.sweep(db)
    db.expire_all()
    assert db.get(Recording, rec_id).status == Status.QUEUED
    assert db.get(Recording, abandoned.id).status == Status.FAILED
    assert "did not complete" in db.get(Recording, abandoned.id).error_message


def test_job_that_keeps_crashing_is_eventually_failed(api, db):
    rec_id = upload(api, SAMPLES / "sample_en.wav")
    rec = db.get(Recording, rec_id)
    rec.attempts = worker.MAX_ATTEMPTS
    db.commit()
    assert worker.claim_next_job(db) is None
    db.expire_all()
    rec = db.get(Recording, rec_id)
    assert rec.status == Status.FAILED and rec.can_retry
