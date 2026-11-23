import random

import pytest

from app.solver import SolverError, solve_beat_grid


def brute_force(onset, n_beats, min_gap, max_gap, confirmed, delta=2):
    n_frames = len(onset)
    best = None
    lines = []
    first = confirmed[0]
    last = confirmed[n_beats - 1]

    def rec(beat, positions):
        nonlocal best, lines
        if beat == n_beats - 1:
            if positions[-1] != last:
                return
            score = sum(onset[p] for p in positions)
            if best is None or score > best:
                best = score
                lines = [tuple(positions)]
            elif score == best:
                lines.append(tuple(positions))
            return
        lo = min_gap
        hi = max_gap
        if beat >= 1:
            prev_gap = positions[-1] - positions[-2]
            lo = max(lo, prev_gap - delta)
            hi = min(hi, prev_gap + delta)
        if beat + 1 == n_beats - 1:
            options = [last]
        elif beat + 1 in confirmed:
            options = [confirmed[beat + 1]]
        else:
            options = range(positions[-1] + lo, positions[-1] + hi + 1)
        for nxt in options:
            if nxt <= positions[-1] or nxt >= n_frames:
                continue
            gap = nxt - positions[-1]
            if not (lo <= gap <= hi):
                continue
            if beat + 1 in confirmed and nxt != confirmed[beat + 1]:
                continue
            positions.append(nxt)
            rec(beat + 1, positions)
            positions.pop()

    rec(0, [first])
    if best is None:
        return None
    candidates = [set() for _ in range(n_beats)]
    for line in lines:
        for i, p in enumerate(line):
            candidates[i].add(p)
    return best, [tuple(sorted(c)) for c in candidates]


def test_simple_case():
    onset = [0] * 20
    onset[2] = 10
    onset[7] = 20
    onset[12] = 30
    onset[17] = 40
    result = solve_beat_grid(onset, 4, 5, 5, {0: 2, 3: 17})
    assert result.total_score == 100
    assert result.candidates == ((2,), (7,), (12,), (17,))
    assert result.ambiguous_measures == ()


@pytest.mark.parametrize("seed", range(60))
def test_random_against_brute_force(seed):
    rng = random.Random(seed)
    n_frames = rng.randint(14, 26)
    n_beats = 4
    min_gap = rng.randint(2, 4)
    max_gap = rng.randint(min_gap, min(min_gap + 3, 7))
    onset = [rng.choice([0, 0, 5, 10, 10, 100, 1000]) for _ in range(n_frames)]

    first = rng.randint(0, 3)
    # choose a reachable last frame sometimes, sometimes random
    if rng.random() < 0.7:
        total = sum(rng.randint(min_gap, max_gap) for _ in range(n_beats - 1))
        last = first + total
    else:
        last = rng.randint(first + 1, n_frames - 1)
    if last >= n_frames:
        pytest.skip("generated last frame outside range")

    confirmed = {0: first, n_beats - 1: last}
    # optionally confirm one middle beat
    if rng.random() < 0.5:
        mid = rng.choice([1, 2])
        options = list(range(first + 1, last))
        if options:
            confirmed[mid] = rng.choice(options)

    expected = brute_force(onset, n_beats, min_gap, max_gap, confirmed)

    if expected is None:
        with pytest.raises(SolverError):
            solve_beat_grid(onset, n_beats, min_gap, max_gap, confirmed)
        return

    result = solve_beat_grid(onset, n_beats, min_gap, max_gap, confirmed)
    exp_score, exp_candidates = expected
    assert result.total_score == exp_score
    assert list(result.candidates) == [tuple(c) for c in exp_candidates]


@pytest.mark.parametrize("seed", range(20))
def test_random_8_beats_against_brute_force(seed):
    rng = random.Random(1000 + seed)
    n_frames = rng.randint(30, 44)
    n_beats = 8
    min_gap = rng.randint(3, 5)
    max_gap = rng.randint(min_gap, min_gap + 2)
    onset = [rng.choice([0, 0, 0, 5, 10, 10, 100]) for _ in range(n_frames)]

    # build a guaranteed-reachable skeleton with random gaps
    gaps = [rng.randint(min_gap, max_gap) for _ in range(n_beats - 1)]
    first = rng.randint(0, 2)
    positions = [first]
    for g in gaps:
        positions.append(positions[-1] + g)
    if positions[-1] >= n_frames:
        pytest.skip("skeleton outside range")
    confirmed = {0: first, n_beats - 1: positions[-1]}
    if rng.random() < 0.6:
        idx = rng.choice([1, 2, 3, 4, 5, 6])
        confirmed[idx] = positions[idx]

    expected = brute_force(onset, n_beats, min_gap, max_gap, confirmed)
    if expected is None:
        with pytest.raises(SolverError):
            solve_beat_grid(onset, n_beats, min_gap, max_gap, confirmed)
        return
    result = solve_beat_grid(onset, n_beats, min_gap, max_gap, confirmed)
    exp_score, exp_candidates = expected
    assert result.total_score == exp_score
    assert list(result.candidates) == [tuple(c) for c in exp_candidates]


def test_no_solution_raises():
    onset = [1] * 20
    # last beat cannot be reached from first with 3 gaps in [2,3]
    with pytest.raises(SolverError):
        solve_beat_grid(onset, 4, 2, 3, {0: 0, 3: 19})


def test_confirmed_order_validation():
    with pytest.raises(ValueError):
        solve_beat_grid([1] * 20, 4, 2, 3, {0: 10, 3: 5})
    with pytest.raises(ValueError):
        solve_beat_grid([1] * 20, 4, 2, 3, {4: 1, 3: 5})
