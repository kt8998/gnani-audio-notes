"""
The background worker: a separate process (`python -m app.worker.run`).

Postgres is the job queue. A job is just a recordings row with status 'queued'.

    loop:
        every minute: re-queue stale jobs, expire abandoned uploads
        claim the oldest queued recording (SELECT ... FOR UPDATE SKIP LOCKED)
        run the pipeline on it
        nothing to do? sleep 2 s

SKIP LOCKED means that if we ever run two workers, they can't grab the same job:
a row locked by one worker is simply invisible to the other's query.
"""

import logging
import signal
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import Recording
from app.services.gnani import GnaniClient
from app.services.summarizer import Summarizer
from app.status import WORKING_STATES, Status, transition
from app.worker.pipeline import process_recording

log = logging.getLogger("worker")

POLL_SECONDS = 2
SWEEP_EVERY_SECONDS = 60
MAX_ATTEMPTS = 3  # worker pick-ups per job before we give up (protects against crash loops)
# The pipeline updates locked_at after every chunk, so a job this quiet means its worker died.
STALE_AFTER = timedelta(minutes=10)
ABANDONED_UPLOAD_AFTER = timedelta(hours=1)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def claim_next_job(db: Session) -> uuid.UUID | None:
    recording = db.scalars(
        select(Recording)
        .where(Recording.status == Status.QUEUED)
        .order_by(Recording.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        # Always use the row as just locked in Postgres, never a stale copy cached in this session.
        .execution_options(populate_existing=True)
    ).first()
    if recording is None:
        db.rollback()
        return None

    if recording.attempts >= MAX_ATTEMPTS:
        transition(recording, Status.FAILED)
        recording.error_message = "Processing was interrupted repeatedly. You can retry."
        recording.error_detail = f"gave up after {recording.attempts} worker attempts"
        db.commit()
        return None

    transition(recording, Status.PREPROCESSING)
    recording.attempts += 1
    recording.locked_at = utcnow()
    db.commit()  # releases the row lock; the status change is what keeps others away now
    return recording.id


def sweep(db: Session) -> None:
    # A worker died mid-job (crash, redeploy): put the job back in the queue.
    stale = db.scalars(
        select(Recording)
        .where(Recording.status.in_(list(WORKING_STATES)), Recording.locked_at < utcnow() - STALE_AFTER)
        .with_for_update(skip_locked=True)
    ).all()
    for recording in stale:
        log.warning("re-queueing stale job %s (was %s)", recording.id, recording.status)
        transition(recording, Status.QUEUED)
        recording.locked_at = None

    # The browser created the row but never finished uploading.
    abandoned = db.scalars(
        select(Recording)
        .where(Recording.status == Status.UPLOADING, Recording.created_at < utcnow() - ABANDONED_UPLOAD_AFTER)
        .with_for_update(skip_locked=True)
    ).all()
    for recording in abandoned:
        transition(recording, Status.FAILED)
        recording.error_message = "The upload did not complete. Please upload the file again."
    db.commit()


class Worker:
    def __init__(self):
        settings = get_settings()
        self.gnani = GnaniClient(settings.gnani_api_key, settings.gnani_stt_url)
        self.summarizer = Summarizer()
        self.chunk_seconds = settings.chunk_seconds
        self.concurrency = settings.transcribe_concurrency
        self.stopping = False
        self.last_sweep = 0.0

    def run_once(self) -> bool:
        """One loop iteration. Returns True if a job was processed."""
        if time.monotonic() - self.last_sweep > SWEEP_EVERY_SECONDS:
            with SessionLocal() as db:
                sweep(db)
            self.last_sweep = time.monotonic()

        with SessionLocal() as db:
            job_id = claim_next_job(db)
        if job_id is None:
            return False

        log.info("processing %s", job_id)
        with SessionLocal() as db:
            process_recording(
                db, job_id,
                gnani=self.gnani, summarizer=self.summarizer,
                chunk_seconds=self.chunk_seconds, concurrency=self.concurrency,
            )
        log.info("finished %s", job_id)
        return True

    def run_forever(self) -> None:
        # Railway sends SIGTERM on redeploy: finish the current job, then exit.
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        log.info("worker started")
        while not self.stopping:
            try:
                if not self.run_once():
                    time.sleep(POLL_SECONDS)
            except Exception:
                # e.g. database briefly unreachable. Don't die; try again shortly.
                log.exception("worker loop error")
                time.sleep(POLL_SECONDS * 5)
        log.info("worker stopped")

    def _stop(self, *_):
        log.info("stop requested; finishing current job first")
        self.stopping = True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Worker().run_forever()
