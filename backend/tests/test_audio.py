"""Audio tests run the real bundled ffmpeg binary on real files."""

import io
import wave
from pathlib import Path

import pytest

from app.services import audio
from app.services.audio import AudioError

SAMPLES = Path(__file__).resolve().parent.parent / "scripts" / "samples"
HUMAN_M4A = Path(__file__).resolve().parents[2] / "test-human.m4a"


def test_plan_chunks_exact_multiple():
    assert audio.plan_chunks(50.0, 25) == [(0.0, 25.0), (25.0, 50.0)]


def test_plan_chunks_normal_tail():
    assert audio.plan_chunks(60.0, 25) == [(0.0, 25.0), (25.0, 50.0), (50.0, 60.0)]


def test_plan_chunks_tiny_tail_is_merged_and_stays_under_30s():
    chunks = audio.plan_chunks(51.0, 25)
    assert chunks == [(0.0, 25.0), (25.0, 51.0)]
    assert all(end - start < 30 for start, end in chunks)


def test_plan_chunks_short_file():
    assert audio.plan_chunks(1.5, 25) == [(0.0, 1.5)]


def test_decode_wav_and_split(tmp_path):
    out = tmp_path / "out.wav"
    duration = audio.decode_to_wav(SAMPLES / "long_75s_en.wav", out)
    assert duration == pytest.approx(68.9, abs=0.2)

    chunks = audio.plan_chunks(duration, 25)
    assert len(chunks) == 3
    pieces = [audio.read_chunk(out, s, e) for s, e in chunks]
    total = 0.0
    for data, (s, e) in zip(pieces, chunks):
        with wave.open(io.BytesIO(data)) as w:
            assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
            length = w.getnframes() / 16000
            assert length == pytest.approx(e - s, abs=0.001)
            total += length
    assert total == pytest.approx(duration, abs=0.01)  # nothing lost, nothing duplicated


@pytest.mark.skipif(not HUMAN_M4A.exists(), reason="test-human.m4a not present")
def test_decode_real_m4a(tmp_path):
    duration = audio.decode_to_wav(HUMAN_M4A, tmp_path / "out.wav")
    assert 1 < duration < 120


def test_corrupt_file_raises_friendly_error(tmp_path):
    bad = tmp_path / "broken.mp3"
    bad.write_bytes(b"this is definitely not audio" * 100)
    with pytest.raises(AudioError) as info:
        audio.decode_to_wav(bad, tmp_path / "out.wav")
    assert "corrupted" in info.value.user_message
    assert info.value.detail  # ffmpeg's stderr is kept for debugging
