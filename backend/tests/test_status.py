import pytest

from app.status import InvalidTransition, Status, transition


class Rec:
    def __init__(self, status):
        self.status = status


def test_happy_path():
    r = Rec(Status.UPLOADING)
    for s in [Status.QUEUED, Status.PREPROCESSING, Status.TRANSCRIBING, Status.SUMMARIZING, Status.COMPLETED]:
        transition(r, s)
    assert r.status == Status.COMPLETED


def test_retry_after_failure():
    r = Rec(Status.TRANSCRIBING)
    transition(r, Status.FAILED)
    transition(r, Status.QUEUED)
    assert r.status == Status.QUEUED


@pytest.mark.parametrize(
    "start,target",
    [
        (Status.COMPLETED, Status.QUEUED),  # finished work is never re-run
        (Status.UPLOADING, Status.TRANSCRIBING),  # can't skip the queue
        (Status.QUEUED, Status.COMPLETED),
        (Status.FAILED, Status.COMPLETED),
    ],
)
def test_illegal_transitions_are_rejected(start, target):
    with pytest.raises(InvalidTransition):
        transition(Rec(start), target)
