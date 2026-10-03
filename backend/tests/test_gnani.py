"""
GnaniClient behaviour. Responses replayed here are the exact bodies we got from the
live API during verification (success, silence, >30 s, bad key).
"""

import httpx
import pytest

from app.services.gnani import GnaniAuthError, GnaniClient, GnaniError

SUCCESS = {"success": True, "request_id": "req-1", "transcript": "hello world", "model": "gnani-prisma-v2.5"}
SILENCE = {"success": True, "request_id": "req-2", "transcript": "", "model": "gnani-prisma-v2.5"}
TOO_LONG = {"success": False, "error": {"type": "MAX_AUDIO_DURATION_EXCEEDED", "message": "Audio duration exceeds maximum allowed duration of 30 seconds"}}
BAD_KEY = {"detail": {"error_code": "INVALID_API_KEY", "message": "Unauthorized", "status_code": 401}}


def make_client(responses):
    """Client whose HTTP layer returns the given responses in order. Records requests and sleeps."""
    calls, sleeps = [], []
    queue = list(responses)

    def handler(request: httpx.Request):
        calls.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx.Response(status, json=body)

    client = GnaniClient("test-key", "https://gnani.test/stt/v3", http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append)
    return client, calls, sleeps


def test_success_sends_documented_request():
    client, calls, _ = make_client([(200, SUCCESS)])
    result = client.transcribe(b"RIFF....", "en-IN")
    assert (result.transcript, result.request_id, result.attempts) == ("hello world", "req-1", 1)
    req = calls[0]
    assert req.headers["X-API-Key-ID"] == "test-key"
    body = req.content
    assert b'name="audio_file"' in body and b'name="language_code"' in body and b"en-IN" in body


def test_silence_is_a_valid_empty_transcript():
    client, _, _ = make_client([(200, SILENCE)])
    assert client.transcribe(b"x", "en-IN").transcript == ""


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_are_not_retried(status):
    client, calls, sleeps = make_client([(status, BAD_KEY)])
    with pytest.raises(GnaniAuthError) as info:
        client.transcribe(b"x", "en-IN")
    assert len(calls) == 1 and sleeps == []
    assert "INVALID_API_KEY" in str(info.value)


def test_400_uses_the_other_error_format_and_is_not_retried():
    client, calls, _ = make_client([(400, TOO_LONG)])
    with pytest.raises(GnaniError) as info:
        client.transcribe(b"x", "en-IN")
    assert info.value.error_type == "MAX_AUDIO_DURATION_EXCEEDED"
    assert info.value.retryable is False and len(calls) == 1


def test_transient_errors_retry_with_exponential_backoff_then_succeed():
    client, calls, sleeps = make_client([(429, {}), (503, {}), (200, SUCCESS)])
    result = client.transcribe(b"x", "en-IN")
    assert result.transcript == "hello world" and result.attempts == 3
    assert sleeps == [2.0, 4.0]


def test_timeouts_and_connection_errors_are_retried():
    client, calls, sleeps = make_client([httpx.ReadTimeout("slow"), httpx.ConnectError("down"), (200, SUCCESS)])
    assert client.transcribe(b"x", "en-IN").attempts == 3


def test_retries_are_bounded():
    client, calls, sleeps = make_client([(500, {}), (500, {}), (500, {})])
    with pytest.raises(GnaniError) as info:
        client.transcribe(b"x", "en-IN")
    assert len(calls) == 3 and info.value.attempts == 3 and info.value.retryable
