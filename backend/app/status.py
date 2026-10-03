"""
The recording lifecycle, in one place.

    uploading ──► queued ──► preprocessing ──► transcribing ──► summarizing ──► completed
        │            ▲            │                 │                │
        └──► failed ◄┴────────────┴─────────────────┴────────────────┘
                 │
                 └──► queued   (user clicks Retry)

The three "working" states can also go back to queued when a worker dies
mid-job and the stale-job sweeper re-queues it.
"""

from enum import StrEnum


class Status(StrEnum):
    UPLOADING = "uploading"  # row created, browser is PUTting the file to storage
    QUEUED = "queued"  # file is in storage, waiting for a worker
    PREPROCESSING = "preprocessing"  # worker is downloading/decoding/splitting
    TRANSCRIBING = "transcribing"  # chunks are being sent to Gnani
    SUMMARIZING = "summarizing"  # transcript done, LLM is summarizing
    COMPLETED = "completed"
    FAILED = "failed"


WORKING_STATES = {Status.PREPROCESSING, Status.TRANSCRIBING, Status.SUMMARIZING}
TERMINAL_STATES = {Status.COMPLETED, Status.FAILED}

ALLOWED_TRANSITIONS: dict[Status, set[Status]] = {
    Status.UPLOADING: {Status.QUEUED, Status.FAILED},
    Status.QUEUED: {Status.PREPROCESSING, Status.FAILED},
    # Any working state may jump straight to summarizing/completed when a retried
    # job finds that earlier stages are already done (idempotent resume).
    Status.PREPROCESSING: {Status.TRANSCRIBING, Status.SUMMARIZING, Status.COMPLETED, Status.FAILED, Status.QUEUED},
    Status.TRANSCRIBING: {Status.SUMMARIZING, Status.FAILED, Status.QUEUED},
    Status.SUMMARIZING: {Status.COMPLETED, Status.FAILED, Status.QUEUED},
    Status.FAILED: {Status.QUEUED},
    Status.COMPLETED: set(),
}


class InvalidTransition(Exception):
    pass


def transition(recording, new_status: Status) -> None:
    """Change recording.status, refusing moves the lifecycle doesn't allow."""
    current = Status(recording.status)
    if new_status not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"cannot go from {current} to {new_status}")
    recording.status = new_status
