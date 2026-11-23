"""Strict WAV input/output helpers for the cue-track service."""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2  # bytes -> 16 bit PCM
CHANNELS = 1
FRAME_SAMPLES = 160  # 10 ms at 16 kHz
MAX_SECONDS = 30
MAX_SAMPLES = SAMPLE_RATE * MAX_SECONDS


class AudioValidationError(ValueError):
    """The uploaded audio does not match the required format."""


@dataclass(frozen=True)
class AudioInfo:
    samples: np.ndarray  # int16, mono
    n_frames: int  # number of 10 ms onset frames
    duration_seconds: float


def read_wav(data: bytes) -> AudioInfo:
    """Parse and validate a 16 kHz / 16-bit / mono PCM WAV payload."""

    if not data:
        raise AudioValidationError("audio file is empty")
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            rate = wf.getframerate()
            comptype = wf.getcomptype()
            n_samples = wf.getnframes()
            raw = wf.readframes(n_samples)
    except (wave.Error, EOFError) as exc:
        raise AudioValidationError(f"not a readable PCM WAV file: {exc}") from exc

    if comptype != "NONE":
        raise AudioValidationError("WAV must use uncompressed PCM encoding")
    if channels != CHANNELS:
        raise AudioValidationError(f"WAV must be mono, got {channels} channel(s)")
    if width != SAMPLE_WIDTH:
        raise AudioValidationError(f"WAV must be 16-bit, got {width * 8}-bit")
    if rate != SAMPLE_RATE:
        raise AudioValidationError(f"WAV must be 16000 Hz, got {rate} Hz")
    if n_samples == 0:
        raise AudioValidationError("audio file contains no samples")
    if n_samples > MAX_SAMPLES:
        raise AudioValidationError(
            f"audio must be at most {MAX_SECONDS} s, got {n_samples / SAMPLE_RATE:.3f} s"
        )
    if len(raw) != n_samples * SAMPLE_WIDTH:
        raise AudioValidationError("WAV payload is truncated")

    samples = np.frombuffer(raw, dtype="<i2").astype(np.int16, copy=True)
    n_frames = (n_samples + FRAME_SAMPLES - 1) // FRAME_SAMPLES
    return AudioInfo(
        samples=samples,
        n_frames=n_frames,
        duration_seconds=n_samples / SAMPLE_RATE,
    )


def write_wav(samples: np.ndarray, path: str) -> None:
    """Write mono 16-bit PCM at 16 kHz."""

    with wave.open(path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples.astype("<i2", copy=False).tobytes())
