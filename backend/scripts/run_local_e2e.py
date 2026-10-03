"""
Live end-to-end run of the real backend on one audio file:
    API create -> PUT to storage -> API confirm -> worker -> real Gnani -> real Gemini

Uses DATABASE_URL, GNANI_API_KEY and GEMINI_API_KEY from backend/.env.
If S3_ENDPOINT_URL is empty (no real bucket configured), a local S3-compatible server
(moto) is started as a stand-in, and the script says so.

Usage (from backend/):
    python scripts/run_local_e2e.py ../test-human.m4a
    python scripts/run_local_e2e.py scripts/samples/long_75s_en.wav --language en-IN
"""

import argparse
import os
import socket
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from dotenv import dotenv_values  # noqa: E402

import app  # noqa: E402,F401  (first: sets up TLS before boto3/moto are imported)

CLIENT_ID = "local-e2e-script"


def start_local_s3_if_needed() -> bool:
    if (os.environ.get("S3_ENDPOINT_URL") or dotenv_values(BACKEND / ".env").get("S3_ENDPOINT_URL")):
        return False
    import boto3
    from moto.server import ThreadedMotoServer

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    os.environ.update(
        S3_ENDPOINT_URL=f"http://127.0.0.1:{port}", S3_ACCESS_KEY_ID="local", S3_SECRET_ACCESS_KEY="local",
        S3_BUCKET="local-bucket", S3_REGION="us-east-1",
    )
    ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False).start()
    boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT_URL"], aws_access_key_id="local",
                 aws_secret_access_key="local", region_name="us-east-1").create_bucket(Bucket="local-bucket")

    from app.config import get_settings

    get_settings.cache_clear()  # settings were already read (by `import app`) before these env vars existed
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio")
    parser.add_argument("--language", default="en-IN")
    args = parser.parse_args()
    path = Path(args.audio).resolve()

    local_s3 = start_local_s3_if_needed()
    print("storage:", "LOCAL S3 STAND-IN (moto), not the real bucket" if local_s3 else "configured bucket (S3_ENDPOINT_URL)")

    import httpx
    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient

    from app.db import SessionLocal
    from app.main import app
    from app.models import Recording
    from app.worker.run import Worker

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(cfg, "head")

    api = TestClient(app)
    headers = {"X-Client-Id": CLIENT_ID}
    data = path.read_bytes()
    r = api.post("/api/recordings", headers=headers, json={
        "filename": path.name, "content_type": "application/octet-stream",
        "size_bytes": len(data), "language_code": args.language,
    })
    r.raise_for_status()
    created = r.json()
    rec_id = created["recording"]["id"]
    print(f"created recording {rec_id}  ({len(data)} bytes)")

    put = httpx.put(created["upload_url"], content=data, headers=created["upload_headers"], timeout=120)
    print("upload PUT ->", put.status_code)
    put.raise_for_status()
    print("confirm ->", api.post(f"/api/recordings/{rec_id}/uploaded", headers=headers).json()["status"])

    if local_s3:
        # This laptop's antivirus resets plain-HTTP localhost downloads over ~400 KB, so in
        # stand-in mode the worker reads the original from disk instead of fetching it back.
        import shutil

        from app.services import storage

        storage.download_to = lambda key, dst: shutil.copyfile(path, dst)
        print("note: worker reads the original from local disk (local stand-in mode only)")

    started = time.perf_counter()
    worker = Worker()
    while not worker.run_once():  # claim this (or an older queued) job and process it
        time.sleep(1)
    elapsed = time.perf_counter() - started

    with SessionLocal() as db:
        rec = db.get(Recording, rec_id)
        print(f"\nstatus: {rec.status}   duration: {rec.duration_seconds:.1f}s   processing: {elapsed:.1f}s"
              if rec.duration_seconds else f"\nstatus: {rec.status}")
        print(f"chunks: {rec.chunks_done}/{rec.chunks_total}")
        for c in rec.chunks:
            print(f"  [{c.idx}] {c.start_seconds:6.1f}-{c.end_seconds:6.1f}s  {c.status:7} attempts={c.attempts} "
                  f"req={c.gnani_request_id}  {(c.transcript or '')[:70]!r}")
        if rec.error_message:
            print("\nERROR (user-facing):", rec.error_message)
            print("detail:", (rec.error_detail or "")[:500])
        print("\nTRANSCRIPT:\n", rec.transcript)
        print("\nSUMMARY:\n", rec.summary)
        return 0 if rec.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
