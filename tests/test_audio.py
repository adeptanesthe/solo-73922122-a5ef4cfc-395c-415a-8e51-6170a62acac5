"""Tests for WAV validation, cue rendering and worst-case solver timing."""

from __future__ import annotations

import io
import time
import wave

import numpy as np
import pytest

from app.cue import render_cue_track
from app.solver import SolverError, solve_beat_grid
from app.wav_io import FRAME_SAMPLES, MAX_SAMPLES, AudioValidationError, read_wav

from .test_api import make_wav


def wav_with(n_samples=16000, rate=16000, channels=1, width=2) -> bytes:
    raw = np.zeros(n_samples * channels, dtype="<i2" if width == 2 else "<u1").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(raw)
    return buffer.getvalue()


def test_frame_count_rounds_up_for_partial_frame():
    info = read_wav(make_wav(n_samples=FRAME_SAMPLES * 300 + 1))
    assert info.n_frames == 301


def test_full_30_seconds_accepted():
    info = read_wav(make_wav(n_samples=MAX_SAMPLES))
    assert info.duration_seconds == 30.0
    assert info.n_frames == 3000


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"n_samples": MAX_SAMPLES + 1}, "at most 30 s"),
        ({"rate": 44100}, "16000 Hz"),
        ({"channels": 2}, "mono"),
        ({"width": 1}, "16-bit"),
        ({"n_samples": 0}, "no samples"),
    ],
)
def test_invalid_wav_rejected(kwargs, fragment):
    if kwargs.get("n_samples") == 0:
        data = wav_with(0)
    else:
        data = wav_with(**kwargs)
    with pytest.raises(AudioValidationError, match=fragment):
        read_wav(data)


def test_non_pcm_wav_rejected():
    # hand-written header claiming IEEE float encoding
    data = b"RIFF" + (36).to_bytes(4, "little") + b"WAVE"
    data += b"fmt " + (16).to_bytes(4, "little") + (3).to_bytes(2, "little")
    data += (1).to_bytes(2, "little") + (16000).to_bytes(4, "little")
    data += (32000).to_bytes(4, "little") + (2).to_bytes(2, "little")
    data += (16).to_bytes(2, "little") + b"data" + (0).to_bytes(4, "little")
    with pytest.raises(AudioValidationError):
        read_wav(data)


def test_render_silences_measures_not_cued_and_clips():
    positions = [0, 10, 20, 30, None, 50, 60, 70]
    track = render_cue_track(160 * 100, positions, {1})
    assert track.dtype == np.int16
    assert track.size == 160 * 100
    assert np.max(np.abs(track[30 * 160 + 1000 :])) == 0
    assert np.max(np.abs(track[: 30 * 160])) > 0


def test_render_never_overflows_int16():
    # place an accented click at sample zero and check the value is representable
    track = render_cue_track(1000, [0, None, None, None], {1})
    assert track[0] == 0  # attack ramp starts at zero
    assert int(np.max(np.abs(track))) < 32768


def test_solver_worst_case_runtime():
    rng = np.random.default_rng(7)
    scores = rng.integers(0, 1001, size=3000).tolist()
    start = time.perf_counter()
    result = solve_beat_grid(scores, 32, 20, 100, {0: 0, 31: 2999})
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0
    assert len(result.candidates) == 32


def test_solver_reachable_solution_with_partial_last_frame():
    # last frame rounding: a recording whose final frame is only 1 sample long
    info = read_wav(make_wav(n_samples=FRAME_SAMPLES * 12 + 1))
    scores = [0] * info.n_frames
    for f in (0, 4, 8, 12):
        scores[f] = 10
    result = solve_beat_grid(scores, 4, 4, 4, {0: 0, 3: 12})
    assert result.candidates == ((0,), (4,), (8,), (12,))


def test_solver_rejects_too_few_beats_and_bad_inputs():
    with pytest.raises(ValueError):
        solve_beat_grid([0, 1], 1, 1, 2, {0: 0})
    with pytest.raises(ValueError):
        solve_beat_grid([0, 1], 2, 0, 2, {0: 0, 1: 1})
    with pytest.raises(ValueError):
        solve_beat_grid([0, -1], 2, 1, 2, {0: 0, 1: 1})
    with pytest.raises(ValueError):
        solve_beat_grid([0, 1], 2, 1, 2, {0: 0})
    with pytest.raises(SolverError):
        solve_beat_grid([0, 0, 0], 2, 1, 1, {0: 0, 1: 2})
