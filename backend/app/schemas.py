"""Shapes of what the API accepts and returns (separate from the DB models on purpose:
internal fields like error_detail, storage_key and owner_id are never sent to the browser)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CreateRecordingRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="", max_length=100)
    size_bytes: int = Field(gt=0)
    language_code: str


class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    idx: int
    start_seconds: float
    end_seconds: float
    status: str
    transcript: str | None


class RecordingListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    language_code: str
    status: str
    duration_seconds: float | None
    chunks_total: int
    chunks_done: int
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None


class RecordingOut(RecordingListItem):
    size_bytes: int
    transcript: str | None
    summary: str | None
    can_retry: bool
    updated_at: datetime
    # Lets the UI show the transcript filling in while chunks finish.
    chunks: list[ChunkOut]


class CreateRecordingResponse(BaseModel):
    recording: RecordingOut
    upload_url: str
    # The browser must send exactly these headers with its PUT, or the signature won't match.
    upload_headers: dict[str, str]


class AudioUrlResponse(BaseModel):
    url: str
