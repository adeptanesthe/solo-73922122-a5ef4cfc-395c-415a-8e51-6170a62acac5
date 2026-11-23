#!/usr/bin/env python3
"""Create demo inputs for the cue-track API using only the standard library.

Outputs two scenarios:

* ``take.wav`` / ``annotation.json`` / ``reannotation.json`` /
  ``reannotation_bad.json`` — the original two-measure tour.
* ``take3.wav`` / ``annotation3.json`` / ``reannotation_move.json`` — a
  three-measure recording used to demonstrate the incremental re-solve:
  moving the confirmed beat 5 by two frames flips measure 3 (behind the
  unchanged confirmed beat 9) from blank to cued.
"""

from __future__ import annotations

import json
import math
import struct
import sys
import wave

RATE = 16_000
FRAME = 160

# ---------------------------------------------------------------- two measures
SECONDS = 3
N_SAMPLES = RATE * SECONDS
N_FRAMES = (N_SAMPLES + FRAME - 1) // FRAME

# beat frames for two 4/4 measures at roughly 600 ms spacing
BEATS = [10, 50, 90, 130, 170, 210, 250, 290]

# A second onset right next to beat 5 makes measure 2 ambiguous: the solver can
# place beat 5 on either frame at no score cost.
TOLERANT = {171: 100}

# -------------------------------------------------------------- three measures
# 12 beats on a 40-frame grid starting at frame 10 (4.8 s of audio).
SECONDS3 = 4.8
N_SAMPLES3 = int(RATE * SECONDS3)
N_FRAMES3 = (N_SAMPLES3 + FRAME - 1) // FRAME
BEATS3 = [10 + 40 * i for i in range(12)]

# Decoy onsets.  In version 1 (beat 5 confirmed at frame 170) the best prefix
# reaches the confirmed beat 9 (frame 330) with a 40-frame gap, so the ±2-frame
# neighbour rule lets beat 10 sit on 369 or 370 with equal score -> measure 3
# stays blank.  Moving beat 5 to frame 172 shifts the best prefix: the gap into
# beat 9 becomes 42 frames, so the gap out of beat 9 must be >= 40 and beat 10
# is locked to frame 370 -> measure 3 gains clicks, although the confirmed
# beat 9 between the moved beat and measure 3 never changed.
DECOYS3 = {208: 110, 248: 120, 288: 90, 333: 120, 369: 100, 411: 90}


def synth(n_samples: int, beat_frames: list[int]) -> list[int]:
    samples = []
    for i in range(n_samples):
        t = i / RATE
        value = 0.10 * math.sin(2 * math.pi * 220 * t)
        samples.append(int(value * 32767))
    for beat in beat_frames:
        # audible click in the "recording" itself
        for j in range(int(0.02 * RATE)):
            idx = beat * FRAME + j
            if idx < n_samples:
                samples[idx] = min(
                    32767,
                    samples[idx] + int(9000 * math.exp(-j / 200.0)),
                )
    return samples


def write_wav(path: str, samples: list[int]) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def write_json(path: str, payload: dict) -> None:
    with open(path, "w") as fh:
        json.dump(payload, fh)


def main(outdir: str) -> None:
    # --- two-measure scenario (original demo tour)
    scores = [0] * N_FRAMES
    for beat in BEATS:
        scores[beat] = 100
    for frame, value in TOLERANT.items():
        scores[frame] = value

    wav_path = f"{outdir}/take.wav"
    write_wav(wav_path, synth(N_SAMPLES, BEATS))

    write_json(
        f"{outdir}/annotation.json",
        {
            "measure_count": 2,
            "min_gap_frames": 38,
            "max_gap_frames": 46,
            "confirmed_beats": [
                {"beat": 1, "frame": 10},
                {"beat": 8, "frame": 290},
            ],
            "onset_scores": scores,
        },
    )
    write_json(
        f"{outdir}/reannotation.json",
        {
            "confirmed_beats": [
                {"beat": 1, "frame": 10},
                {"beat": 5, "frame": 170},
                {"beat": 8, "frame": 290},
            ]
        },
    )
    write_json(
        f"{outdir}/reannotation_bad.json",
        {
            "confirmed_beats": [
                {"beat": 1, "frame": 10},
                {"beat": 8, "frame": 40},
            ]
        },
    )

    # --- three-measure scenario (incremental re-solve demo)
    scores3 = [0] * N_FRAMES3
    for beat in BEATS3:
        scores3[beat] = 100
    for frame, value in DECOYS3.items():
        scores3[frame] = value

    wav3_path = f"{outdir}/take3.wav"
    write_wav(wav3_path, synth(N_SAMPLES3, BEATS3))

    write_json(
        f"{outdir}/annotation3.json",
        {
            "measure_count": 3,
            "min_gap_frames": 38,
            "max_gap_frames": 46,
            "confirmed_beats": [
                {"beat": 1, "frame": 10},
                {"beat": 5, "frame": 170},
                {"beat": 9, "frame": 330},
                {"beat": 12, "frame": 450},
            ],
            "onset_scores": scores3,
        },
    )
    # Move exactly one confirmed beat (beat 5: 170 -> 172); beats 1, 9 and 12
    # stay put.  Measure 3 flips from blank to cued.
    write_json(
        f"{outdir}/reannotation_move.json",
        {
            "confirmed_beats": [
                {"beat": 1, "frame": 10},
                {"beat": 5, "frame": 172},
                {"beat": 9, "frame": 330},
                {"beat": 12, "frame": 450},
            ]
        },
    )

    print(wav_path)
    print(wav3_path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
