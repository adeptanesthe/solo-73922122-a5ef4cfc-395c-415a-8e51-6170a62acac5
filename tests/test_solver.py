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


# --------------------------------------------------------------- incremental


def random_skeleton(rng, n_beats, min_gap, max_gap, n_frames):
    """A reachable random beat skeleton starting at a low frame."""
    first = rng.randint(0, 2)
    positions = [first]
    for _ in range(n_beats - 1):
        positions.append(positions[-1] + rng.randint(min_gap, max_gap))
    if positions[-1] >= n_frames:
        return None
    return positions


@pytest.mark.parametrize("seed", range(40))
def test_incremental_move_matches_full_solve(seed):
    """Moving one confirmed mark must reproduce the full solve exactly."""
    rng = random.Random(5000 + seed)
    n_frames = rng.randint(50, 80)
    n_beats = 8
    min_gap = rng.randint(3, 5)
    max_gap = rng.randint(min_gap, min_gap + 3)
    onset = [rng.choice([0, 0, 5, 10, 100]) for _ in range(n_frames)]

    positions = random_skeleton(rng, n_beats, min_gap, max_gap, n_frames)
    if positions is None:
        pytest.skip("skeleton outside range")
    mid = rng.choice([2, 3, 4, 5])
    confirmed1 = {0: positions[0], mid: positions[mid], n_beats - 1: positions[-1]}
    r1 = solve_beat_grid(onset, n_beats, min_gap, max_gap, confirmed1)

    delta = rng.choice([-2, -1, 1, 2])
    moved = positions[mid] + delta
    if not confirmed1[0] < moved < confirmed1[n_beats - 1]:
        pytest.skip("moved mark outside range")
    confirmed2 = dict(confirmed1)
    confirmed2[mid] = moved

    try:
        r2_full = solve_beat_grid(onset, n_beats, min_gap, max_gap, confirmed2)
    except SolverError:
        with pytest.raises(SolverError):
            solve_beat_grid(onset, n_beats, min_gap, max_gap, confirmed2, reuse=r1.state)
        return
    r2_inc = solve_beat_grid(
        onset, n_beats, min_gap, max_gap, confirmed2, reuse=r1.state
    )
    assert r2_inc.total_score == r2_full.total_score
    assert r2_inc.candidates == r2_full.candidates
    assert r2_inc.ambiguous_beats == r2_full.ambiguous_beats
    assert r2_inc.ambiguous_measures == r2_full.ambiguous_measures
    # an interior move reuses at least one recursion level on each side
    assert r2_inc.changed_beats == (mid,)
    assert r2_inc.forward_levels_reused >= 1
    assert r2_inc.backward_levels_reused >= 1
    total_levels = n_beats - 1
    assert r2_inc.forward_levels_computed + r2_inc.forward_levels_reused == total_levels
    assert r2_inc.backward_levels_computed + r2_inc.backward_levels_reused == total_levels
    assert r2_inc.forward_levels_computed < r2_full.forward_levels_computed
    assert r2_inc.backward_levels_computed < r2_full.backward_levels_computed


@pytest.mark.parametrize("seed", range(30))
def test_incremental_add_and_remove_mark_matches_full_solve(seed):
    rng = random.Random(7000 + seed)
    n_frames = rng.randint(50, 80)
    n_beats = 8
    min_gap = rng.randint(3, 5)
    max_gap = rng.randint(min_gap, min_gap + 3)
    onset = [rng.choice([0, 0, 5, 10, 100]) for _ in range(n_frames)]

    positions = random_skeleton(rng, n_beats, min_gap, max_gap, n_frames)
    if positions is None:
        pytest.skip("skeleton outside range")
    mid = rng.choice([1, 2, 3, 4, 5, 6])
    base = {0: positions[0], n_beats - 1: positions[-1]}
    with_mid = dict(base)
    with_mid[mid] = positions[mid]

    r_with = solve_beat_grid(onset, n_beats, min_gap, max_gap, with_mid)
    r_base = solve_beat_grid(onset, n_beats, min_gap, max_gap, base)

    # remove the middle mark, reusing the state that included it
    try:
        expected = solve_beat_grid(onset, n_beats, min_gap, max_gap, base)
    except SolverError:
        with pytest.raises(SolverError):
            solve_beat_grid(onset, n_beats, min_gap, max_gap, base, reuse=r_with.state)
        return
    got = solve_beat_grid(onset, n_beats, min_gap, max_gap, base, reuse=r_with.state)
    assert got.total_score == expected.total_score
    assert got.candidates == expected.candidates
    assert got.changed_beats == (mid,)

    # add the mark back, reusing the state that lacked it
    got2 = solve_beat_grid(onset, n_beats, min_gap, max_gap, with_mid, reuse=r_base.state)
    assert got2.total_score == r_with.total_score
    assert got2.candidates == r_with.candidates
    assert got2.changed_beats == (mid,)


def test_incremental_identical_marks_reuse_everything():
    onset = [0, 5, 0, 0, 10, 0, 0, 20, 0, 0, 30, 0, 0, 40, 0, 0, 50, 0, 0, 60]
    confirmed = {0: 1, 2: 10, 3: 15}
    r1 = solve_beat_grid(onset, 4, 4, 6, confirmed)
    r2 = solve_beat_grid(onset, 4, 4, 6, confirmed, reuse=r1.state)
    assert r2.total_score == r1.total_score
    assert r2.candidates == r1.candidates
    assert r2.changed_beats == ()
    assert r2.forward_levels_computed == 0
    assert r2.backward_levels_computed == 0
    assert r2.forward_levels_reused == 3
    assert r2.backward_levels_reused == 3


def test_incompatible_state_falls_back_to_full_solve():
    onset = [0, 5, 0, 0, 10, 0, 0, 20, 0, 0, 30, 0, 0, 40, 0, 0, 50, 0, 0, 60]
    r1 = solve_beat_grid(onset, 4, 4, 6, {0: 1, 3: 19})
    # different beat count -> the state cannot be reused
    r2 = solve_beat_grid(onset, 5, 3, 5, {0: 1, 4: 19}, reuse=r1.state)
    assert r2.changed_beats is None
    assert r2.forward_levels_reused == 0
    assert r2.backward_levels_reused == 0
    # different gap range -> the state cannot be reused either
    r3 = solve_beat_grid(onset, 4, 5, 6, {0: 1, 3: 19}, reuse=r1.state)
    assert r3.changed_beats is None
    assert r3.forward_levels_reused == 0
    # same shape but different onset scores -> the state cannot be reused
    other_onset = [1] * 20
    r4 = solve_beat_grid(other_onset, 4, 4, 6, {0: 1, 3: 19}, reuse=r1.state)
    assert r4.changed_beats is None
    assert r4.forward_levels_reused == 0


def test_reused_state_is_read_only():
    onset = [0, 5, 0, 0, 10, 0, 0, 20, 0, 0, 30, 0, 0, 40, 0, 0, 50, 0, 0, 60]
    r1 = solve_beat_grid(onset, 4, 4, 6, {0: 1, 3: 19})
    assert r1.state is not None
    for tables in (r1.state.forward, r1.state.backward):
        for rows in tables:
            if rows is None:
                continue
            for row in rows:
                assert not row.flags.writeable
    # reusing the state must not corrupt it: solving twice gives equal results
    confirmed2 = {0: 1, 2: 11, 3: 16}
    r2 = solve_beat_grid(onset, 4, 4, 6, confirmed2, reuse=r1.state)
    r3 = solve_beat_grid(onset, 4, 4, 6, confirmed2, reuse=r1.state)
    assert r2.total_score == r3.total_score
    assert r2.candidates == r3.candidates


@pytest.mark.parametrize("seed", range(20))
def test_incremental_endpoint_moves_match_full_solve(seed):
    """Moving the first or last confirmed mark exercises the reuse boundary."""
    rng = random.Random(9000 + seed)
    n_frames = rng.randint(50, 80)
    n_beats = 8
    min_gap = rng.randint(3, 5)
    max_gap = rng.randint(min_gap, min_gap + 3)
    onset = [rng.choice([0, 0, 5, 10, 100]) for _ in range(n_frames)]

    positions = random_skeleton(rng, n_beats, min_gap, max_gap, n_frames)
    if positions is None:
        pytest.skip("skeleton outside range")
    confirmed1 = {0: positions[0], 3: positions[3], n_beats - 1: positions[-1]}
    r1 = solve_beat_grid(onset, n_beats, min_gap, max_gap, confirmed1)

    confirmed2 = dict(confirmed1)
    if rng.random() < 0.5:
        confirmed2[0] = confirmed1[0] + rng.choice([1, 2])
    else:
        confirmed2[n_beats - 1] = confirmed1[n_beats - 1] - rng.choice([1, 2])
    if not confirmed2[0] < confirmed2[3] < confirmed2[n_beats - 1]:
        pytest.skip("endpoint move collapsed the order")

    try:
        r2_full = solve_beat_grid(onset, n_beats, min_gap, max_gap, confirmed2)
    except SolverError:
        with pytest.raises(SolverError):
            solve_beat_grid(onset, n_beats, min_gap, max_gap, confirmed2, reuse=r1.state)
        return
    r2_inc = solve_beat_grid(
        onset, n_beats, min_gap, max_gap, confirmed2, reuse=r1.state
    )
    assert r2_inc.total_score == r2_full.total_score
    assert r2_inc.candidates == r2_full.candidates
    if 0 in r2_inc.changed_beats:
        # moving the first mark recomputes every forward level but reuses
        # all backward levels (they never depend on beat 0)
        assert r2_inc.forward_levels_reused == 0
        assert r2_inc.backward_levels_reused == n_beats - 1
    else:
        # moving the last mark recomputes every backward level but reuses
        # all forward levels except the last beat's
        assert r2_inc.forward_levels_reused == n_beats - 2
        assert r2_inc.backward_levels_reused == 0
