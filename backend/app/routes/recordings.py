"""
HTTP endpoints. Everything here is fast and synchronous; the slow work
(decoding, Gnani, LLM) happens in the worker process.

"Ownership" without accounts: the browser generates a random id once, keeps it
in localStorage and sends it as X-Client-Id. Listing and changing recordings
require it, so nobody sees a global list of other people's uploads. Reading one
recording only needs its UUID (unguessable), so a link can be shared.
"""

import uuid
from datetime import datetime, timezone
from pathlib import PurePath

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import schemas
from app.config import get_settings
from app.db import get_db
from app.languages import LANGUAGES
from app.models import Recording
from app.services import storage
from app.status import Status, transition

router = APIRouter(prefix="/api")

ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".aac", ".webm", ".mp4"}

ClientId = Header(alias="X-Client-Id", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$")


def _get(db: Session, recording_id: uuid.UUID) -> Recording:
    recording = db.get(Recording, recording_id)
    if recording is None:
        raise HTTPException(404, "Recording not found.")
    return recording


def _get_owned(db: Session, recording_id: uuid.UUID, client_id: str) -> Recording:
    recording = _get(db, recording_id)
    if recording.owner_id != client_id:
        raise HTTPException(404, "Recording not found.")  # don't reveal that it exists
    return recording


@router.post("/recordings", response_model=schemas.CreateRecordingResponse, status_code=201)
def create_recording(body: schemas.CreateRecordingRequest, client_id: str = ClientId, db: Session = Depends(get_db)):
    """Step 1 of an upload: validate, create the row, hand back a presigned PUT URL."""
    settings = get_settings()
    ext = PurePath(body.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type '{ext or 'none'}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}.")
    if body.size_bytes > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(400, f"File is too large. The limit is {settings.max_upload_mb} MB.")
    if body.language_code not in LANGUAGES:
        raise HTTPException(400, f"Unsupported language '{body.language_code}'.")

    content_type = body.content_type or "application/octet-stream"
    recording_id = uuid.uuid4()
    recording = Recording(
        id=recording_id,
        owner_id=client_id,
        original_filename=body.filename,
        content_type=content_type,
        size_bytes=body.size_bytes,
        storage_key=f"uploads/{recording_id}/original{ext}",
        language_code=body.language_code,
        status=Status.UPLOADING,
    )
    db.add(recording)
    db.commit()
    db.refresh(recording)

    return schemas.CreateRecordingResponse(
        recording=schemas.RecordingOut.model_validate(recording),
        upload_url=storage.presign_upload(recording.storage_key, content_type),
        upload_headers={"Content-Type": content_type},
    )


@router.post("/recordings/{recording_id}/uploaded", response_model=schemas.RecordingOut)
def mark_uploaded(recording_id: uuid.UUID, client_id: str = ClientId, db: Session = Depends(get_db)):
    """Step 2 of an upload: the browser says the PUT finished. We check the bucket, then queue the job."""
    recording = _get_owned(db, recording_id, client_id)
    if recording.status == Status.QUEUED:
        return recording  # already confirmed; calling twice is harmless
    if recording.status != Status.UPLOADING:
        raise HTTPException(409, f"Recording is already {recording.status}.")

    size = storage.object_size(recording.storage_key)
    if size is None:
        raise HTTPException(400, "The file was not found in storage. The upload may have failed; please try again.")
    if size != recording.size_bytes:
        transition(recording, Status.FAILED)
        recording.error_message = "The upload was incomplete. Please upload the file again."
        recording.error_detail = f"expected {recording.size_bytes} bytes, storage has {size}"
        db.commit()
        raise HTTPException(400, recording.error_message)

    transition(recording, Status.QUEUED)
    recording.uploaded_at = datetime.now(timezone.utc)
    db.commit()
    return recording


@router.get("/recordings", response_model=list[schemas.RecordingListItem])
def list_recordings(client_id: str = ClientId, db: Session = Depends(get_db)):
    """Only this browser's recordings, newest first."""
    return db.scalars(
        select(Recording).where(Recording.owner_id == client_id).order_by(Recording.created_at.desc()).limit(100)
    ).all()


@router.get("/recordings/{recording_id}", response_model=schemas.RecordingOut)
def get_recording(recording_id: uuid.UUID, db: Session = Depends(get_db)):
    """Polled every ~2s by the UI while processing, so it must stay cheap."""
    return _get(db, recording_id)


@router.post("/recordings/{recording_id}/retry", response_model=schemas.RecordingOut)
def retry_recording(recording_id: uuid.UUID, client_id: str = ClientId, db: Session = Depends(get_db)):
    """Re-queue a failed job. The worker skips whatever already succeeded."""
    recording = _get_owned(db, recording_id, client_id)
    if not recording.can_retry:
        raise HTTPException(409, "Only failed recordings whose file was uploaded can be retried.")
    transition(recording, Status.QUEUED)
    recording.attempts = 0
    recording.error_message = None
    recording.error_detail = None
    db.commit()
    return recording


@router.get("/recordings/{recording_id}/audio", response_model=schemas.AudioUrlResponse)
def get_audio_url(recording_id: uuid.UUID, db: Session = Depends(get_db)):
    """Short-lived link so the UI can play the original file from the private bucket."""
    recording = _get(db, recording_id)
    if recording.uploaded_at is None:
        raise HTTPException(404, "The audio file has not been uploaded.")
    return {"url": storage.presign_download(recording.storage_key)}


@router.get("/languages")
def list_languages():
    return [{"code": code, "name": name} for code, name in LANGUAGES.items()]
