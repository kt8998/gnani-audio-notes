"""
Summarizer logic with a stand-in Gemini client: no network, no API key, no quota used.
The stand-in returns the SDK's *real* response/error types, so `.text`, finish reasons
and error classes behave exactly as in production.
"""

from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors, types

from app.services import summarizer as summarizer_module
from app.services.summarizer import NO_SPEECH_SUMMARY, Summarizer, SummaryError


def gemini_response(text="## Overview\nA test.", finish=types.FinishReason.STOP, block_reason=None):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]), finish_reason=finish)]
        if block_reason is None else [],
        prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason=block_reason) if block_reason else None,
    )


class FakeModels:
    def __init__(self, result):
        self.calls, self.result = [], result

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make(result=None, model="gemini-3.8-flash"):
    models = FakeModels(result if result is not None else gemini_response())
    return Summarizer(client=SimpleNamespace(models=models), model=model), models


def test_empty_transcript_skips_the_llm():
    s, models = make()
    assert s.summarize("   ", "en-IN") == NO_SPEECH_SUMMARY
    assert models.calls == []


def test_normal_transcript_is_one_call_with_prompt_and_language_hint():
    s, models = make()
    assert s.summarize("namaste aap kaise hain", "hi-IN") == "## Overview\nA test."
    assert len(models.calls) == 1
    call = models.calls[0]
    assert call["model"] == "gemini-3.8-flash"
    assert "Hindi" in call["contents"] and "namaste aap kaise hain" in call["contents"]
    instruction = call["config"].system_instruction
    assert "same language as the transcript" in instruction
    assert "Do not invent" in instruction
    assert call["config"].thinking_config.thinking_level == types.ThinkingLevel.LOW


def test_older_models_get_no_thinking_level():
    s, models = make(model="gemini-2.5-flash")
    s.summarize("hello", "en-IN")
    assert models.calls[0]["config"].thinking_config is None


def test_very_long_transcript_is_summarized_in_parts_not_truncated(monkeypatch):
    monkeypatch.setattr(summarizer_module, "MAX_CHARS_PER_REQUEST", 100)
    s, models = make()
    s.summarize("word " * 50, "en-IN")  # 250 chars -> 3 parts + 1 combine
    assert len(models.calls) == 4
    assert "part 1 of 3" in models.calls[0]["contents"]
    assert "Combine them" in models.calls[-1]["contents"]


@pytest.mark.parametrize(
    "result,expected",
    [
        (gemini_response(finish=types.FinishReason.SAFETY), "declined"),
        (gemini_response(block_reason=types.BlockedReason.PROHIBITED_CONTENT), "declined"),
        (gemini_response(text=""), "empty response"),
        (errors.ClientError(429, {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}}), "quota"),
        (errors.ClientError(403, {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}}), "credentials"),
        (errors.ClientError(400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}), "credentials"),
        (errors.ClientError(404, {"error": {"code": 404, "message": "model not found", "status": "NOT_FOUND"}}), "model was not found"),
        (errors.ServerError(503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}}), "You can retry"),
        (httpx.ConnectError("no route"), "Could not reach"),
    ],
)
def test_failures_become_user_friendly_summary_errors(result, expected):
    s, _ = make(result)
    with pytest.raises(SummaryError) as info:
        s.summarize("some text", "en-IN")
    assert expected in info.value.user_message
    assert info.value.detail  # technical detail kept for debugging, never shown to users


def test_real_client_is_configured_with_retries_and_timeout():
    client = summarizer_module.make_client("fake-key")  # constructing a client makes no network call
    options = client._api_client._http_options
    assert options.timeout == summarizer_module.REQUEST_TIMEOUT_MS
    assert options.retry_options.attempts == 4
