"""
What the worker does with one recording.

    download original from bucket -> ffmpeg decode -> split into 25 s chunks
    -> Gnani per chunk (3 in parallel) -> join transcript -> Gemini summary -> completed

Idempotent by design: every stage checks whether its output already exists.
  * transcript already saved?  skip transcription entirely
  * chunk rows already exist?  reuse them (same boundaries), only send chunks not yet 'done'
  * summary already saved?     skip the LLM call
So a retry after a failure or a worker crash never redoes (or pays for) finished work,
and never creates duplicate rows.

Threads: only the Gnani HTTP calls run in the thread pool. All database writes
happen on this (main) thread, because a SQLAlchemy session is not thread-safe.
"""

import logging
import tempfile
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from boto3.exceptions import Boto3Error
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.orm import Session

from app.models import Chunk, Recording
from app.services import audio, storage
from app.services.audio import AudioError
from app.services.gnani import GnaniAuthError, GnaniClient, GnaniError
from app.services.summarizer import Summarizer, SummaryError
from app.status import Status, transition

log = logging.getLogger(__name__)


class PipelineError(Exception):
    def __init__(self, user_message: str, detail: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


def now() -> datetime:
    return datetime.now(timezone.utc)


def process_recording(
    db: Session,
    recording_id: uuid.UUID,
    *,
    gnani: GnaniClient,
    summarizer: Summarizer,
    chunk_seconds: float,
    concurrency: int,
) -> None:
    """Run (or resume) the pipeline. Never raises: failures are written to the row."""
    recording = db.get(Recording, recording_id)
    try:
        _run(db, recording, gnani, summarizer, chunk_seconds, concurrency)
    except (PipelineError, AudioError, SummaryError) as exc:
        log.warning("recording %s failed: %s", recording_id, exc.user_message)
        _fail(db, recording_id, exc.user_message, exc.detail)
    except Exception:
        log.exception("recording %s crashed", recording_id)
        _fail(db, recording_id, "Something went wrong while processing this recording. You can retry.", traceback.format_exc())


def _run(db, recording, gnani, summarizer, chunk_seconds, concurrency) -> None:
    # Stage 1+2: audio -> transcript
    if recording.transcript is None:
        chunks_finished = recording.chunks and all(c.status == "done" for c in recording.chunks)
        if not chunks_finished:
            _transcribe(db, recording, gnani, chunk_seconds, concurrency)
        recording.transcript = " ".join(c.transcript for c in recording.chunks if c.transcript)
        db.commit()

    # Stage 3: transcript -> summary
    if recording.summary is None:
        transition(recording, Status.SUMMARIZING)
        recording.locked_at = now()
        db.commit()
        recording.summary = summarizer.summarize(recording.transcript, recording.language_code)
        db.commit()

    transition(recording, Status.COMPLETED)
    recording.completed_at = now()
    recording.locked_at = None
    recording.error_message = None
    recording.error_detail = None
    db.commit()


def _transcribe(db, recording, gnani, chunk_seconds, concurrency) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        original = Path(tmp) / "original"
        wav = Path(tmp) / "audio.wav"

        try:
            storage.download_to(recording.storage_key, original)
        # ClientError: S3 said no (e.g. 404). BotoCoreError: network. Boto3Error: download retries exhausted.
        except (ClientError, BotoCoreError, Boto3Error) as exc:
            raise PipelineError("Could not read the uploaded file from storage. You can retry.", str(exc))

        duration = audio.decode_to_wav(original, wav)  # raises AudioError for corrupt files
        recording.duration_seconds = duration

        if not recording.chunks:  # first run: create chunk rows. Retries reuse them.
            for idx, (start, end) in enumerate(audio.plan_chunks(duration, chunk_seconds)):
                recording.chunks.append(Chunk(idx=idx, start_seconds=start, end_seconds=end, status="pending"))
        recording.chunks_total = len(recording.chunks)
        recording.chunks_done = sum(c.status == "done" for c in recording.chunks)
        transition(recording, Status.TRANSCRIBING)
        recording.locked_at = now()
        db.commit()

        todo = [c for c in recording.chunks if c.status != "done"]
        for c in todo:
            c.status, c.error_message = "pending", None
        auth_error = None

        def transcribe_slice(start: float, end: float):
            # Runs in a worker thread: reads its own slice, calls Gnani, touches no DB state.
            return gnani.transcribe(audio.read_chunk(wav, start, end), recording.language_code)

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(transcribe_slice, c.start_seconds, c.end_seconds): c for c in todo}
            for future in as_completed(futures):
                chunk = futures[future]
                try:
                    result = future.result()
                    chunk.transcript = result.transcript  # "" = no speech, still a success
                    chunk.gnani_request_id = result.request_id
                    chunk.attempts += result.attempts
                    chunk.status = "done"
                except GnaniAuthError as exc:
                    chunk.status, chunk.error_message = "failed", str(exc)
                    chunk.attempts += exc.attempts
                    auth_error = exc
                    pool.shutdown(wait=False, cancel_futures=True)  # no point sending the rest
                except GnaniError as exc:
                    chunk.status, chunk.error_message = "failed", str(exc)
                    chunk.attempts += exc.attempts

                # Progress + heartbeat after every chunk, so the UI moves and the
                # stale-job sweeper knows this worker is alive.
                recording.chunks_done = sum(c.status == "done" for c in recording.chunks)
                recording.locked_at = now()
                db.commit()
                if auth_error:
                    break

    if auth_error:
        raise PipelineError(
            "The transcription service rejected our credentials. This is a server configuration problem.",
            str(auth_error),
        )
    failed = [c for c in recording.chunks if c.status != "done"]
    if failed:
        raise PipelineError(
            f"Transcription failed for {len(failed)} of {len(recording.chunks)} segments after several attempts. "
            "You can retry; segments that already worked won't be redone.",
            "; ".join(f"chunk {c.idx}: {c.error_message}" for c in failed),
        )


def _fail(db: Session, recording_id: uuid.UUID, user_message: str, detail: str) -> None:
    db.rollback()  # discard any half-written state from the failed step
    recording = db.get(Recording, recording_id)
    transition(recording, Status.FAILED)
    recording.error_message = user_message
    recording.error_detail = detail[-5000:] if detail else None
    recording.locked_at = None
    db.commit()
