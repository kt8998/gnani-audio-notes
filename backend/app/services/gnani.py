"""
Gnani REST Speech-to-Text client.

Behaviour below was verified against the live API (not just the docs):
  * POST https://api.vachana.ai/stt/v3, multipart: audio_file + language_code (+ format)
  * auth header X-API-Key-ID
  * 200 -> {"success": true, "request_id": ..., "transcript": "..."}
  * silence -> 200 with transcript "" (valid: no speech, NOT an error)
  * > 30 s audio -> 400 {"success": false, "error": {"type": "MAX_AUDIO_DURATION_EXCEEDED", "message": ...}}
  * bad key -> 401 {"detail": {"error_code": "INVALID_API_KEY", "message": "Unauthorized", ...}}
    (docs only mention 403; we treat both as auth failures)

Retry policy:
  * 429, 5xx, timeouts, connection errors -> retry, with exponential backoff (2s, 4s), max 3 attempts
  * 401/403 -> never retry: the key is wrong, retrying just burns time
  * other 4xx -> never retry: the same request will fail the same way
"""

import time
from dataclasses import dataclass

import httpx

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
AUTH_STATUS = {401, 403}


@dataclass
class TranscriptionResult:
    transcript: str  # may be "" when the audio had no speech
    request_id: str | None
    attempts: int


class GnaniError(Exception):
    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None, error_type: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.error_type = error_type
        self.attempts = 1


class GnaniAuthError(GnaniError):
    """401/403: configuration problem on our side. Stops the whole job."""


def parse_error(response: httpx.Response) -> tuple[str | None, str]:
    """Gnani uses two error shapes; return (error_type, message) from either."""
    try:
        body = response.json()
    except ValueError:
        return None, response.text[:300]
    if isinstance(body, dict):
        if isinstance(body.get("error"), dict):  # {"success": false, "error": {"type", "message"}}
            return body["error"].get("type"), body["error"].get("message") or ""
        detail = body.get("detail")
        if isinstance(detail, dict):  # {"detail": {"error_code", "message", "status_code"}}
            return detail.get("error_code"), detail.get("message") or ""
        if isinstance(detail, str):
            return None, detail
    return None, str(body)[:300]


class GnaniClient:
    def __init__(
        self,
        api_key: str,
        url: str,
        *,
        http: httpx.Client | None = None,
        max_attempts: int = 3,
        base_delay: float = 2.0,
        sleep=time.sleep,
    ):
        self.api_key = api_key
        self.url = url
        # httpx.Client is thread-safe, so one client is shared by all worker threads.
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.sleep = sleep

    def transcribe(self, wav_bytes: bytes, language_code: str) -> TranscriptionResult:
        """Transcribe one chunk (<30 s), retrying transient failures."""
        for attempt in range(1, self.max_attempts + 1):
            try:
                transcript, request_id = self._transcribe_once(wav_bytes, language_code)
                return TranscriptionResult(transcript, request_id, attempt)
            except GnaniError as exc:
                exc.attempts = attempt
                if not exc.retryable or attempt == self.max_attempts:
                    raise
                self.sleep(self.base_delay * 2 ** (attempt - 1))  # 2s, 4s, ...
        raise AssertionError("unreachable")

    def _transcribe_once(self, wav_bytes: bytes, language_code: str) -> tuple[str, str | None]:
        try:
            response = self.http.post(
                self.url,
                headers={"X-API-Key-ID": self.api_key},
                files={"audio_file": ("chunk.wav", wav_bytes, "audio/wav")},
                data={"language_code": language_code, "format": "transcribe"},
            )
        except httpx.TimeoutException as exc:
            raise GnaniError(f"timeout: {exc}", retryable=True) from exc
        except httpx.TransportError as exc:  # DNS failure, connection refused/reset, ...
            raise GnaniError(f"connection error: {exc}", retryable=True) from exc

        status = response.status_code
        if status == 200:
            body = response.json()
            if body.get("success") is True and isinstance(body.get("transcript"), str):
                return body["transcript"].strip(), body.get("request_id")
            error_type, message = parse_error(response)
            raise GnaniError(f"unsuccessful 200 response: {message}", retryable=False, status_code=200, error_type=error_type)

        error_type, message = parse_error(response)
        text = f"HTTP {status} {error_type or ''}: {message}".strip()
        if status in AUTH_STATUS:
            raise GnaniAuthError(text, retryable=False, status_code=status, error_type=error_type)
        raise GnaniError(text, retryable=status in RETRYABLE_STATUS, status_code=status, error_type=error_type)
