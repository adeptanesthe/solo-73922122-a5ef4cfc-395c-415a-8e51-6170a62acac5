"""End-to-end API tests covering the cue rules, versions and downloads."""

from __future__ import annotations

import io
import json
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 160


def make_wav(n_samples: int = 48000, rate: int = 16000, channels: int = 1) -> bytes:
    t = np.arange(n_samples, dtype=np.float64) / SAMPLE_RATE
    tone = (np.sin(2 * np.pi * 220 * t) * 8000).astype("<i2")
    if channels == 2:
        tone = np.repeat(tone, 2)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(tone.tobytes())
    return buffer.getvalue()


def n_frames(n_samples: int = 48000) -> int:
    return (n_samples + FRAME_SAMPLES - 1) // FRAME_SAMPLES


def tolerant_scores(n: int, peaks: dict[int, int]) -> list[int]:
    scores = [0] * n
    for frame, value in peaks.items():
        scores[frame] = value
    return scores


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def post_recording(client, wav, annotation):
    return client.post(
        "/api/recordings",
        files={"audio": ("take.wav", wav, "audio/wav")},
        data={"annotation": json.dumps(annotation)},
    )


# --------------------------------------------------------------- cue semantics

BASE_PEAKS = {10: 100, 50: 100, 90: 100, 130: 100, 170: 100, 171: 100, 210: 100, 250: 100, 290: 100}


def base_annotation():
    return {
        "measure_count": 2,
        "min_gap_frames": 38,
        "max_gap_frames": 46,
        "confirmed_beats": [
            {"beat": 1, "frame": 10},
            {"beat": 8, "frame": 290},
        ],
        "onset_scores": tolerant_scores(n_frames(), BASE_PEAKS),
    }


def test_create_cues_only_unambiguous_measure(client):
    r = post_recording(client, make_wav(), base_annotation())
    assert r.status_code == 201, r.text
    body = r.json()
    version = body["current_version"]
    assert body["current_version"]["ambiguous_measures"] == [2]
    assert version["cued_measures"] == [1]
    assert version["total_score"] == 800
    # beat 5 (index 4) may sit at 170 or 171 in optimal lines
    assert version["beat_positions"][:4] == [10, 50, 90, 130]
    assert version["beat_positions"][4] is None
    assert version["beat_positions"][5:] == [210, 250, 290]


def test_cue_wav_matches_recording_spec_and_accent(client):
    r = post_recording(client, make_wav(), base_annotation())
    body = r.json()
    url = body["current_version"]["download_url"]
    wav = client.get(url)
    assert wav.status_code == 200
    assert wav.headers["content-type"] == "audio/wav"

    with wave.open(io.BytesIO(wav.content), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.getnframes() == 48000  # same length as the recording

    samples = np.frombuffer(wav.content[44:], dtype="<i2")
    # clicks exactly on frames 10, 50, 90, 130 of measure 1
    for frame in (10, 50, 90, 130):
        start = frame * FRAME_SAMPLES
        assert np.max(np.abs(samples[start : start + 480])) > 500
    # measure 2 must stay silent, including frames 170..290
    assert np.max(np.abs(samples[170 * FRAME_SAMPLES : 291 * FRAME_SAMPLES])) == 0

    def peak(frame):
        start = frame * FRAME_SAMPLES
        return int(np.max(np.abs(samples[start : start + 480])))

    assert peak(10) > peak(50)  # downbeat accented


def test_fully_determined_grid_cues_all_measures(client):
    ann = base_annotation()
    ann["onset_scores"][171] = 0
    r = post_recording(client, make_wav(), ann)
    assert r.status_code == 201, r.text
    v = r.json()["current_version"]
    assert v["ambiguous_measures"] == []
    assert v["cued_measures"] == [1, 2]
    assert v["beat_positions"][4] == 170


def test_single_measure(client):
    ann = {
        "measure_count": 1,
        "min_gap_frames": 20,
        "max_gap_frames": 30,
        "confirmed_beats": [{"beat": 1, "frame": 5}, {"beat": 4, "frame": 80}],
        "onset_scores": tolerant_scores(n_frames(), {5: 10, 30: 20, 55: 30, 80: 40}),
    }
    r = post_recording(client, make_wav(), ann)
    assert r.status_code == 201, r.text
    v = r.json()["current_version"]
    assert v["beat_positions"] == [5, 30, 55, 80]
    assert v["total_score"] == 100
    assert v["ambiguous_measures"] == []


# ------------------------------------------------------------------- re-encode

def test_reannotation_keeps_version_on_failure_then_supersedes(client):
    created = post_recording(client, make_wav(), base_annotation()).json()
    rid = created["recording_id"]
    v1_url = f"/api/recordings/{rid}/versions/1/cue.wav"
    assert client.get(v1_url).status_code == 200

    bad = {
        "confirmed_beats": [
            {"beat": 1, "frame": 10},
            {"beat": 8, "frame": 30},  # unreachable with 7 gaps of >=38 frames
        ]
    }
    r = client.post(f"/api/recordings/{rid}/beats", json=bad)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "no_legal_beat_grid"
    assert detail["current_version"] == 1

    meta = client.get(f"/api/recordings/{rid}").json()
    assert meta["current_version_number"] == 1
    assert len(meta["versions"]) == 1
    assert client.get(v1_url).status_code == 200  # still downloadable

    good = {
        "confirmed_beats": [
            {"beat": 1, "frame": 10},
            {"beat": 5, "frame": 171},
            {"beat": 8, "frame": 290},
        ]
    }
    r = client.post(f"/api/recordings/{rid}/beats", json=good)
    assert r.status_code == 201, r.text
    v2 = r.json()
    assert v2["version"] == 2
    assert v2["is_current"] is True
    assert v2["ambiguous_measures"] == []
    assert v2["cued_measures"] == [1, 2]
    assert v2["beat_positions"] == [10, 50, 90, 130, 171, 210, 250, 290]

    assert client.get(v1_url).status_code == 409  # superseded
    v2_url = f"/api/recordings/{rid}/versions/2/cue.wav"
    assert client.get(v2_url).status_code == 200

    meta = client.get(f"/api/recordings/{rid}").json()
    assert [v["version"] for v in meta["versions"]] == [1, 2]
    assert [v["is_current"] for v in meta["versions"]] == [False, True]


def test_reannotation_missing_endpoint_keeps_version(client):
    created = post_recording(client, make_wav(), base_annotation()).json()
    rid = created["recording_id"]
    r = client.post(
        f"/api/recordings/{rid}/beats",
        json={"confirmed_beats": [{"beat": 1, "frame": 10}, {"beat": 4, "frame": 130}]},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "missing_endpoint_marks"
    assert client.get(f"/api/recordings/{rid}").json()["current_version_number"] == 1


def test_reannotation_out_of_range_frame_keeps_version(client):
    created = post_recording(client, make_wav(), base_annotation()).json()
    rid = created["recording_id"]
    r = client.post(
        f"/api/recordings/{rid}/beats",
        json={
            "confirmed_beats": [
                {"beat": 1, "frame": 10},
                {"beat": 8, "frame": 10_000},
            ]
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_annotation"
    assert client.get(f"/api/recordings/{rid}").json()["current_version_number"] == 1


# ------------------------------------------------------------------ validation

@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda a: a.update(measure_count=9), "invalid_annotation"),
        (lambda a: a.update(measure_count=0), "invalid_annotation"),
        (lambda a: a.update(min_gap_frames=19), "invalid_annotation"),
        (lambda a: a.update(max_gap_frames=101), "invalid_annotation"),
        (lambda a: a.update(min_gap_frames=50), "invalid_annotation"),
        (lambda a: a["confirmed_beats"].pop(0), "invalid_annotation"),
        (lambda a: a["confirmed_beats"].__setitem__(1, {"beat": 8, "frame": 10}), "invalid_annotation"),
        (lambda a: a["onset_scores"].__setitem__(3, 1001), "invalid_annotation"),
        (lambda a: a["onset_scores"].pop(), "frame_count_mismatch"),
    ],
)
def test_annotation_validation(client, mutate, expected):
    ann = base_annotation()
    mutate(ann)
    r = post_recording(client, make_wav(), ann)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == expected


def test_bad_audio_formats(client):
    ann = base_annotation()

    stereo = post_recording(client, make_wav(channels=2), ann)
    assert stereo.status_code == 422
    assert "mono" in stereo.json()["detail"]["message"]

    wrong_rate = post_recording(client, make_wav(rate=8000), ann)
    assert wrong_rate.status_code == 422
    assert "16000" in wrong_rate.json()["detail"]["message"]

    long = post_recording(client, make_wav(n_samples=16000 * 31), ann)
    assert long.status_code == 422

    junk = post_recording(client, b"not a wav", ann)
    assert junk.status_code == 422
    assert junk.json()["detail"]["code"] == "invalid_audio"


def test_annotation_must_be_json_object(client):
    r = client.post(
        "/api/recordings",
        files={"audio": ("a.wav", make_wav(), "audio/wav")},
        data={"annotation": "[1,2,3]"},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_field"


def test_unknown_recording_and_version_404(client):
    assert client.get("/api/recordings/nope").status_code == 404
    assert client.get("/api/recordings/nope/versions/1/cue.wav").status_code == 404
    created = post_recording(client, make_wav(), base_annotation()).json()
    assert (
        client.get(f"/api/recordings/{created['recording_id']}/versions/9/cue.wav").status_code
        == 404
    )


def test_full_30_seconds_eight_measures_end_to_end(client):
    n_samples = 16_000 * 30
    wav = make_wav(n_samples=n_samples)
    frames = (n_samples + FRAME_SAMPLES - 1) // FRAME_SAMPLES
    beats = [10 + i * 85 for i in range(32)]
    ann = {
        "measure_count": 8,
        "min_gap_frames": 80,
        "max_gap_frames": 90,
        "confirmed_beats": [
            {"beat": 1, "frame": beats[0]},
            {"beat": 32, "frame": beats[-1]},
        ],
        "onset_scores": tolerant_scores(frames, {f: 100 for f in beats}),
    }
    r = post_recording(client, wav, ann)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["audio"]["onset_frames"] == frames
    v = body["current_version"]
    assert v["ambiguous_measures"] == []
    assert len(v["cued_measures"]) == 8
    assert v["total_score"] == 3200
    assert v["beat_positions"] == beats

    cue = client.get(v["download_url"])
    assert cue.status_code == 200
    with wave.open(io.BytesIO(cue.content), "rb") as wf:
        assert wf.getnframes() == n_samples
        assert (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) == (1, 2, 16000)


# ------------------------------------------------------- incremental re-solve

# Three-measure scenario (mirrors scripts/make_demo_inputs.py): moving the
# confirmed beat 5 by two frames flips measure 3 — behind the unchanged
# confirmed beat 9 — from blank to cued.
THREE_MEASURE_FRAMES = 480
THREE_MEASURE_BASE = [10 + 40 * i for i in range(12)]
THREE_MEASURE_DECOYS = {208: 110, 248: 120, 288: 90, 333: 120, 369: 100, 411: 90}


def three_measure_annotation(moved: bool) -> dict:
    peaks = {f: 100 for f in THREE_MEASURE_BASE}
    peaks.update(THREE_MEASURE_DECOYS)
    return {
        "measure_count": 3,
        "min_gap_frames": 38,
        "max_gap_frames": 46,
        "confirmed_beats": [
            {"beat": 1, "frame": 10},
            {"beat": 5, "frame": 172 if moved else 170},
            {"beat": 9, "frame": 330},
            {"beat": 12, "frame": 450},
        ],
        "onset_scores": tolerant_scores(THREE_MEASURE_FRAMES, peaks),
    }


def three_measure_marks(moved: bool) -> dict:
    return {
        "confirmed_beats": [
            {"beat": 1, "frame": 10},
            {"beat": 5, "frame": 172 if moved else 170},
            {"beat": 9, "frame": 330},
            {"beat": 12, "frame": 450},
        ]
    }


def test_moving_one_beat_flips_remote_measure_and_matches_fresh_solve(client):
    wav = make_wav(n_samples=THREE_MEASURE_FRAMES * FRAME_SAMPLES)

    # recording A: solved incrementally (version 1 -> move beat 5 -> version 2)
    created = post_recording(client, wav, three_measure_annotation(moved=False))
    assert created.status_code == 201, created.text
    rid = created.json()["recording_id"]
    v1 = created.json()["current_version"]
    assert v1["ambiguous_measures"] == [3]  # measure 3 blank
    assert v1["cued_measures"] == [1, 2]
    assert v1["beat_positions"][9] is None  # beat 10 tied between two frames

    # download version 1 before the swap; afterwards it must become 409
    cue_v1 = client.get(f"/api/recordings/{rid}/versions/1/cue.wav")
    assert cue_v1.status_code == 200

    r = client.post(f"/api/recordings/{rid}/beats", json=three_measure_marks(moved=True))
    assert r.status_code == 201, r.text
    v2 = r.json()
    assert v2["version"] == 2
    assert v2["ambiguous_measures"] == []  # measure 3 gained clicks
    assert v2["cued_measures"] == [1, 2, 3]
    assert v2["beat_positions"][9] == 370

    # version 1 is superseded after the swap
    assert client.get(f"/api/recordings/{rid}/versions/1/cue.wav").status_code == 409

    # recording B: the moved annotation solved from scratch in one shot
    created_b = post_recording(client, wav, three_measure_annotation(moved=True))
    assert created_b.status_code == 201, created_b.text
    v1_b = created_b.json()["current_version"]

    # score, positions and ambiguity of the incremental re-solve must equal
    # the fresh full solve
    assert v2["total_score"] == v1_b["total_score"]
    assert v2["beat_positions"] == v1_b["beat_positions"]
    assert v2["ambiguous_measures"] == v1_b["ambiguous_measures"]
    assert v2["cued_measures"] == v1_b["cued_measures"]

    # the downloaded cue tracks must be byte-identical
    cue_a = client.get(f"/api/recordings/{rid}/versions/2/cue.wav")
    cue_b = client.get(
        f"/api/recordings/{created_b.json()['recording_id']}/versions/1/cue.wav"
    )
    assert cue_a.status_code == cue_b.status_code == 200
    assert cue_a.content == cue_b.content

    # measure 3 is silent in version 1 and clicks in version 2
    samples_v1 = np.frombuffer(cue_v1.content[44:], dtype="<i2")
    samples_v2 = np.frombuffer(cue_a.content[44:], dtype="<i2")
    m3 = slice(330 * FRAME_SAMPLES, 451 * FRAME_SAMPLES)
    assert np.max(np.abs(samples_v1[m3])) == 0
    assert np.max(np.abs(samples_v2[m3])) > 500


def test_reannotation_uses_current_state_and_keeps_api_shape(client):
    created = post_recording(client, make_wav(), base_annotation()).json()
    rid = created["recording_id"]
    good = {
        "confirmed_beats": [
            {"beat": 1, "frame": 10},
            {"beat": 5, "frame": 171},
            {"beat": 8, "frame": 290},
        ]
    }
    r = client.post(f"/api/recordings/{rid}/beats", json=good)
    assert r.status_code == 201, r.text
    body = r.json()
    # response shape is unchanged
    assert set(body) == {
        "version",
        "is_current",
        "total_score",
        "beat_positions",
        "ambiguous_measures",
        "cued_measures",
        "confirmed_beats",
        "created_at",
        "download_url",
        "recording_id",
        "measure_count",
        "min_gap_frames",
        "max_gap_frames",
    }
    assert body["version"] == 2
    assert body["is_current"] is True
