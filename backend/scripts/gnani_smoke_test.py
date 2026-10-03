"""
Standalone check that the Gnani REST Speech-to-Text API works with our key.

Docs: https://docs.gnani.ai/api/STT/speech-to-text
    POST https://api.vachana.ai/stt/v3   (multipart/form-data)
    Header:  X-API-Key-ID: <api key>
    Fields:  audio_file (file, required), language_code (required), format (optional)

Usage (from the backend/ folder):
    python scripts/gnani_smoke_test.py
    python scripts/gnani_smoke_test.py path/to/clip.wav --language hi-IN --format verbatim

Reads GNANI_API_KEY (and optionally GNANI_STT_URL) from backend/.env or the environment.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Verify TLS against the OS certificate store instead of Python's bundled list.
# Needed on machines where antivirus (e.g. Avast Web Shield) re-signs HTTPS traffic.
# Verification stays ON; this only changes which trusted roots are used.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_URL = "https://api.vachana.ai/stt/v3"
DEFAULT_SAMPLE = Path(__file__).resolve().parent / "samples" / "sample_en.wav"

# Headers worth seeing while we learn the API's behaviour (rate limits are undocumented).
INTERESTING_HEADERS = ("content-type", "x-request-id", "retry-after")


def main() -> int:
    parser = argparse.ArgumentParser(description="Send one audio file to Gnani REST STT.")
    parser.add_argument("audio", nargs="?", default=str(DEFAULT_SAMPLE), help="audio file (<= 60s)")
    parser.add_argument("--language", default="en-IN", help="BCP-47 code, e.g. en-IN, hi-IN")
    parser.add_argument("--format", default="transcribe", choices=["verbatim", "transcribe"])
    args = parser.parse_args()

    load_dotenv(BACKEND_DIR / ".env")
    api_key = os.getenv("GNANI_API_KEY", "").strip()
    url = os.getenv("GNANI_STT_URL", DEFAULT_URL)

    if not api_key:
        print("ERROR: GNANI_API_KEY is not set.")
        print(f"Copy {BACKEND_DIR / '.env.example'} to {BACKEND_DIR / '.env'} and fill in your key.")
        return 2

    audio_path = Path(args.audio)
    if not audio_path.is_file():
        print(f"ERROR: audio file not found: {audio_path}")
        return 2

    print(f"POST {url}")
    print(f"  audio_file    = {audio_path.name} ({audio_path.stat().st_size} bytes)")
    print(f"  language_code = {args.language}")
    print(f"  format        = {args.format}")

    started = time.perf_counter()
    try:
        with audio_path.open("rb") as f:
            response = httpx.post(
                url,
                headers={"X-API-Key-ID": api_key},
                files={"audio_file": (audio_path.name, f, "application/octet-stream")},
                data={"language_code": args.language, "format": args.format},
                timeout=60.0,
            )
    except httpx.HTTPError as exc:
        print(f"\nREQUEST FAILED before a response arrived: {type(exc).__name__}: {exc}")
        return 1
    elapsed = time.perf_counter() - started

    print(f"\nHTTP status: {response.status_code}  ({elapsed:.2f}s)")
    for name in INTERESTING_HEADERS:
        if name in response.headers:
            print(f"  {name}: {response.headers[name]}")
    for name, value in response.headers.items():
        if "ratelimit" in name.lower():
            print(f"  {name}: {value}")

    try:
        body = response.json()
    except ValueError:
        print("\nResponse body (not JSON):")
        print(response.text[:2000])
        return 1

    print("\nResponse body:")
    print(json.dumps(body, indent=2, ensure_ascii=False))

    transcript = body.get("transcript") if isinstance(body, dict) else None
    if response.status_code == 200 and body.get("success") is True and transcript:
        print(f"\nOK: transcript returned ({len(transcript)} characters).")
        return 0

    print("\nFAILED: no transcript in the response.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
