"""Store-level tests: lock-free re-solve and reusable-state lifecycle."""

from __future__ import annotations

import threading

import numpy as np
import pytest

from app.solver import solve_beat_grid
from app.store import ConfirmedMark, Store
from app.wav_io import AudioInfo

ONSET = [0, 5, 0, 0, 10, 0, 0, 20, 0, 0, 30, 0, 0, 40, 0, 0, 50, 0, 0, 60]
MARKS_V1 = [ConfirmedMark(beat=1, frame=1), ConfirmedMark(beat=4, frame=19)]
MARKS_V2 = [
    ConfirmedMark(beat=1, frame=1),
    ConfirmedMark(beat=3, frame=11),
    ConfirmedMark(beat=4, frame=16),
]


def make_audio() -> AudioInfo:
    return AudioInfo(
        samples=np.zeros(160 * len(ONSET), dtype=np.int16),
        n_frames=len(ONSET),
        duration_seconds=0.16 * len(ONSET),
    )


def solve(confirmed):
    return solve_beat_grid(
        ONSET, 4, 4, 6, {m.beat - 1: m.frame for m in confirmed}
    )


def make_store() -> Store:
    store = Store()
    store.create(
        audio=make_audio(),
        onset_scores=ONSET,
        measure_count=1,
        min_gap_frames=4,
        max_gap_frames=6,
        confirmed_marks=MARKS_V1,
        result=solve(MARKS_V1),
        wav=b"v1",
    )
    return store


def only_recording_id(store: Store) -> str:
    with store.lock:
        (rid,) = store._recordings.keys()
    return rid


def test_create_stores_current_solver_state():
    store = make_store()
    rid = only_recording_id(store)
    recording = store.get(rid)
    assert recording.solver_state is not None
    assert recording.solver_state.confirmed == ((0, 1), (3, 19))


def test_state_swaps_with_version_and_old_state_is_dropped():
    store = make_store()
    rid = only_recording_id(store)
    old_state = store.get(rid).solver_state

    def build(recording, prev_state):
        assert prev_state is old_state  # current version's state is handed over
        return solve(MARKS_V2), b"v2"

    recording, version = store.reannotate(rid, MARKS_V2, build)
    assert version.number == 2
    # the recording keeps exactly one state: the new current version's
    assert recording.solver_state is not old_state
    assert recording.solver_state.confirmed == ((0, 1), (2, 11), (3, 16))
    # superseded versions carry no state at all
    assert not hasattr(recording.versions[0], "solver_state")


def test_failed_reannotation_keeps_current_state():
    store = make_store()
    rid = only_recording_id(store)
    old_state = store.get(rid).solver_state

    def build(recording, prev_state):
        raise RuntimeError("no legal grid")

    with pytest.raises(RuntimeError):
        store.reannotate(rid, MARKS_V2, build)
    recording = store.get(rid)
    assert recording.solver_state is old_state
    assert recording.current_version.number == 1
    assert recording.confirmed_marks == MARKS_V1


def test_build_runs_without_holding_the_store_lock():
    """Queries and downloads must work while a re-solve is in progress."""
    store = make_store()
    rid = only_recording_id(store)
    inside_build = threading.Event()
    finish_build = threading.Event()
    probe_results = []
    probe_errors = []

    def build(recording, prev_state):
        inside_build.set()
        assert finish_build.wait(timeout=5)
        return solve(MARKS_V2), b"v2"

    def reannotate():
        store.reannotate(rid, MARKS_V2, build)

    worker = threading.Thread(target=reannotate)
    worker.start()
    assert inside_build.wait(timeout=5)

    def probe():
        try:
            # metadata query and cue download must not block on the re-solve
            probe_results.append(store.snapshot(rid)["current_version_number"])
            version, _ = store.get_version(rid, 1)
            probe_results.append(version.wav)
        except Exception as exc:  # pragma: no cover - failure path
            probe_errors.append(exc)

    for _ in range(3):
        t = threading.Thread(target=probe)
        t.start()
        t.join(timeout=2)
        assert not t.is_alive(), "store query blocked while re-solve was running"
    finish_build.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert probe_errors == []
    assert probe_results == [1, b"v1"] * 3
    # after the swap the new version and its state are in place
    recording = store.get(rid)
    assert recording.current_version.number == 2
    assert recording.solver_state.confirmed == ((0, 1), (2, 11), (3, 16))
