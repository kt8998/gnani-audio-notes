"""
Audio preprocessing.

1. ffmpeg decodes whatever the user uploaded (mp3, m4a, ogg, ...) into ONE
   16 kHz mono 16-bit WAV. That is the format Gnani converts to anyway, and a
   WAV is just a header + raw samples, so the next step is trivial.
2. Python's built-in `wave` module cuts that WAV into ~25 s slices by sample
   position. Same input always gives the same slices, which is what makes
   retries safe: we can re-create "chunk 7" exactly.

Why 25 s: the live Gnani API rejects audio over 30 s (MAX_AUDIO_DURATION_EXCEEDED),
even though the docs say 60 s.
"""

import io
import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg

SAMPLE_RATE = 16_000
# A last slice shorter than this is merged into the previous one instead of
# being sent on its own (25 s + 2 s = 27 s, still under Gnani's 30 s limit).
MIN_TAIL_SECONDS = 2.0
FFMPEG_TIMEOUT_SECONDS = 30 * 60


class AudioError(Exception):
    """The file could not be decoded. `user_message` is safe to show in the UI."""

    def __init__(self, user_message: str, detail: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


def decode_to_wav(src: Path, dst: Path) -> float:
    """Decode any audio file to 16 kHz mono WAV. Returns the duration in seconds."""
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner", "-nostdin", "-y",
        "-i", str(src),
        "-vn",  # ignore any video/cover-art stream
        "-ac", "1",  # mono
        "-ar", str(SAMPLE_RATE),  # 16 kHz
        "-c:a", "pcm_s16le",  # 16-bit PCM
        str(dst),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise AudioError("Decoding the audio took too long.", "ffmpeg timed out")

    if result.returncode != 0:
        raise AudioError(
            "We couldn't read this audio file. It may be corrupted or in an unsupported format.",
            result.stderr[-2000:],
        )

    with wave.open(str(dst), "rb") as w:
        duration = w.getnframes() / w.getframerate()
    if duration < 0.1:
        raise AudioError("This file doesn't contain any audio.", f"decoded duration {duration:.3f}s")
    return duration


def plan_chunks(duration: float, chunk_seconds: float) -> list[tuple[float, float]]:
    """Split [0, duration) into consecutive (start, end) windows of chunk_seconds."""
    chunks = []
    start = 0.0
    while start < duration:
        end = min(start + chunk_seconds, duration)
        chunks.append((start, end))
        start = end
    # Fold a tiny tail into the previous chunk: no point paying for a 0.4 s request.
    if len(chunks) >= 2 and chunks[-1][1] - chunks[-1][0] < MIN_TAIL_SECONDS:
        tail = chunks.pop()
        chunks[-1] = (chunks[-1][0], tail[1])
    return chunks


def read_chunk(wav_path: Path, start: float, end: float) -> bytes:
    """Return the [start, end) slice of a WAV file as a standalone WAV file in memory."""
    with wave.open(str(wav_path), "rb") as src:
        rate = src.getframerate()
        src.setpos(int(start * rate))
        frames = src.readframes(int(round((end - start) * rate)))
        params = src.getparams()

    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setparams(params)
        out.writeframes(frames)
    return buf.getvalue()
