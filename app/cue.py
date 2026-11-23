"""Cue-track synthesis: clicks on unambiguous beats, downbeat accented."""

from __future__ import annotations

import numpy as np

from .wav_io import FRAME_SAMPLES, SAMPLE_RATE

CLICK_SECONDS = 0.03
CLICK_SAMPLES = int(SAMPLE_RATE * CLICK_SECONDS)

ACCENT_FREQ = 1568.0
ACCENT_AMP = 0.90
NORMAL_FREQ = 1046.5
NORMAL_AMP = 0.55
DECAY = 0.011
ATTACK_SECONDS = 0.001


def _click(accent: bool) -> np.ndarray:
    n = CLICK_SAMPLES
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE
    freq = ACCENT_FREQ if accent else NORMAL_FREQ
    amp = ACCENT_AMP if accent else NORMAL_AMP
    envelope = np.exp(-t / DECAY)
    attack = int(ATTACK_SECONDS * SAMPLE_RATE)
    if attack > 0:
        envelope[:attack] *= np.linspace(0.0, 1.0, attack, endpoint=False)
    # Fundamental plus a soft octave for a clearer, wood-block-like timbre.
    wave = np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(4 * np.pi * freq * t)
    return amp * envelope * wave


_ACCENT = _click(True)
_NORMAL = _click(False)


def render_cue_track(
    sample_count: int,
    beat_positions: list[int | None],
    cued_measures: set[int],
) -> np.ndarray:
    """Build an int16 cue track of exactly ``sample_count`` samples.

    ``beat_positions`` holds one 0-based frame per beat (or ``None`` when the
    beat is ambiguous); ``cued_measures`` is a set of 1-based measure numbers
    whose four beats should be audible.  Measures outside the set stay silent.
    """

    track = np.zeros(sample_count, dtype=np.int32)
    for index, frame in enumerate(beat_positions):
        if frame is None:
            continue
        measure = index // 4 + 1
        if measure not in cued_measures:
            continue
        start = frame * FRAME_SAMPLES
        if start >= sample_count:
            continue
        click = _ACCENT if index % 4 == 0 else _NORMAL
        end = min(sample_count, start + len(click))
        track[start:end] += np.round(click[: end - start] * 32767).astype(np.int32)

    return np.clip(track, -32768, 32767).astype(np.int16)
