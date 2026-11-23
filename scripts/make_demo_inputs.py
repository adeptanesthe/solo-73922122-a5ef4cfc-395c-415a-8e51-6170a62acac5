#!/usr/bin/env python3
"""Create demo inputs for the cue-track API using only the standard library."""

from __future__ import annotations

import json
import math
import struct
import sys
import wave

RATE = 16_000
FRAME = 160
SECONDS = 3
N_SAMPLES = RATE * SECONDS
N_FRAMES = (N_SAMPLES + FRAME - 1) // FRAME

# beat frames for two 4/4 measures at roughly 600 ms spacing
BEATS = [10, 50, 90, 130, 170, 210, 250, 290]

# A second onset right next to beat 5 makes measure 2 ambiguous: the solver can
# place beat 5 on either frame at no score cost.
TOLERANT = {171: 100}


def main(outdir: str) -> None:
    samples = []
    for i in range(N_SAMPLES):
        t = i / RATE
        value = 0.10 * math.sin(2 * math.pi * 220 * t)
        samples.append(int(value * 32767))

    scores = [0] * N_FRAMES
    for beat in BEATS:
        scores[beat] = 100
        # audible click in the "recording" itself
        for j in range(int(0.02 * RATE)):
            idx = beat * FRAME + j
            if idx < N_SAMPLES:
                samples[idx] = min(
                    32767,
                    samples[idx] + int(9000 * math.exp(-j / 200.0)),
                )
    for frame, value in TOLERANT.items():
        scores[frame] = value

    wav_path = f"{outdir}/take.wav"
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(struct.pack(f"<{N_SAMPLES}h", *samples))

    annotation = {
        "measure_count": 2,
        "min_gap_frames": 38,
        "max_gap_frames": 46,
        "confirmed_beats": [
            {"beat": 1, "frame": 10},
            {"beat": 8, "frame": 290},
        ],
        "onset_scores": scores,
    }
    with open(f"{outdir}/annotation.json", "w") as fh:
        json.dump(annotation, fh)

    reannotation = {
        "confirmed_beats": [
            {"beat": 1, "frame": 10},
            {"beat": 5, "frame": 170},
            {"beat": 8, "frame": 290},
        ]
    }
    with open(f"{outdir}/reannotation.json", "w") as fh:
        json.dump(reannotation, fh)

    bad_reannotation = {
        "confirmed_beats": [
            {"beat": 1, "frame": 10},
            {"beat": 8, "frame": 40},
        ]
    }
    with open(f"{outdir}/reannotation_bad.json", "w") as fh:
        json.dump(bad_reannotation, fh)

    print(wav_path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
