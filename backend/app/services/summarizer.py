"""
Transcript -> summary with Google Gemini (official `google-genai` SDK, free tier).

One request for normal recordings. The model accepts ~1M input tokens, but we
still split very long transcripts (summarise each part, then summarise the part
summaries) to keep each request small under free-tier per-minute token limits.
We never silently cut a transcript short.

Retries: unlike some SDKs, google-genai does NOT retry by default, so we pass
HttpRetryOptions: 408/429/5xx and network errors are retried with exponential
backoff + jitter.
"""

import httpx
from google import genai
from google.genai import errors, types

from app.config import get_settings
from app.languages import LANGUAGES

MAX_CHARS_PER_REQUEST = 150_000
# Includes the model's (low) thinking tokens as well as the visible summary.
MAX_OUTPUT_TOKENS = 8192
REQUEST_TIMEOUT_MS = 120_000

NO_SPEECH_SUMMARY = "No speech was detected in this recording, so there is nothing to summarize."

SYSTEM_PROMPT = """You summarize transcripts of audio recordings.

The transcript was produced by automatic speech recognition. It is lowercase, has no \
punctuation, may contain misrecognized words, and was transcribed in ~25 second pieces, \
so a word may occasionally be cut at a boundary. Read past these artifacts.

Write the summary in the same language as the transcript. Use Markdown:
- a short overview paragraph (2-4 sentences)
- a "Key points" bulleted list
- an "Action items" list, only if tasks, decisions or follow-ups are actually mentioned

Only state what the transcript supports. Do not invent names, numbers or facts. \
If the transcript is very short or unclear, say so briefly instead of padding."""

# Finish reasons meaning "the model refused / was stopped by a filter" rather than "done".
BLOCKED_FINISH_REASONS = {
    types.FinishReason.SAFETY, types.FinishReason.PROHIBITED_CONTENT, types.FinishReason.BLOCKLIST,
    types.FinishReason.SPII, types.FinishReason.RECITATION,
}


class SummaryError(Exception):
    def __init__(self, user_message: str, detail: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


def make_client(api_key: str) -> genai.Client:
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=REQUEST_TIMEOUT_MS,  # milliseconds in this SDK
            retry_options=types.HttpRetryOptions(attempts=4, initial_delay=2.0, max_delay=30.0),
        ),
    )


class Summarizer:
    def __init__(self, client: genai.Client | None = None, model: str | None = None):
        s = get_settings()
        self.client = client or make_client(s.gemini_api_key)
        self.model = model or s.gemini_model

    # ---- provider-independent part (unchanged from the previous LLM provider) ----

    def summarize(self, transcript: str, language_code: str) -> str:
        transcript = transcript.strip()
        if not transcript:
            return NO_SPEECH_SUMMARY

        language = LANGUAGES.get(language_code, language_code)
        if len(transcript) <= MAX_CHARS_PER_REQUEST:
            return self._ask(f"Recording language: {language}\n\n<transcript>\n{transcript}\n</transcript>")

        # Very long recording: summarise each part, then combine.
        parts = [transcript[i : i + MAX_CHARS_PER_REQUEST] for i in range(0, len(transcript), MAX_CHARS_PER_REQUEST)]
        part_summaries = [
            self._ask(
                f"Recording language: {language}\nThis is part {n} of {len(parts)} of one long recording.\n\n"
                f"<transcript>\n{part}\n</transcript>"
            )
            for n, part in enumerate(parts, start=1)
        ]
        joined = "\n\n".join(f"<part n=\"{n}\">\n{s}\n</part>" for n, s in enumerate(part_summaries, start=1))
        return self._ask(
            f"Recording language: {language}\nBelow are summaries of consecutive parts of one long recording. "
            f"Combine them into a single summary of the whole recording.\n\n{joined}"
        )

    # ---- Gemini-specific part ----

    def _config(self) -> types.GenerateContentConfig:
        config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, max_output_tokens=MAX_OUTPUT_TOKENS)
        if self.model.startswith("gemini-3"):
            # Summarising needs little reasoning; low thinking is faster and uses less quota.
            # (Gemini 3 models take thinking_level; older 2.5 models use a different knob, so we leave them default.)
            config.thinking_config = types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
        return config

    def _ask(self, user_content: str) -> str:
        try:
            response = self.client.models.generate_content(model=self.model, contents=user_content, config=self._config())
        except errors.ClientError as exc:  # 4xx (429 already retried by the SDK)
            if exc.code in (401, 403) or (exc.code == 400 and "API key" in str(exc.message)):
                raise SummaryError("The summary service rejected our credentials (server configuration problem).", str(exc))
            if exc.code == 404:
                raise SummaryError("The configured summary model was not found (server configuration problem).", str(exc))
            if exc.code == 429:
                raise SummaryError("The summary service's free-tier quota is used up for now. Please retry later.", str(exc))
            raise SummaryError("The summary service rejected the request.", str(exc))
        except errors.ServerError as exc:  # 5xx, already retried
            raise SummaryError("The summary service returned an error. You can retry.", str(exc))
        except httpx.HTTPError as exc:  # timeouts / connection failures, already retried
            raise SummaryError("Could not reach the summary service. You can retry.", repr(exc))

        feedback = response.prompt_feedback
        if feedback is not None and feedback.block_reason:
            raise SummaryError("The summary service declined to summarize this content.", str(feedback))

        candidate = response.candidates[0] if response.candidates else None
        finish = candidate.finish_reason if candidate else None
        if finish in BLOCKED_FINISH_REASONS:
            raise SummaryError("The summary service declined to summarize this content.", f"finish_reason={finish}")

        text = (response.text or "").strip()  # .text skips the model's internal "thought" parts
        if not text:
            raise SummaryError("The summary service returned an empty response. You can retry.", f"finish_reason={finish}")
        return text
