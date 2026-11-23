"""Beat-grid solver with reusable forward/backward dynamic-programming state.

The solver finds every beat position that can occur in *some* highest-scoring
legal beat line.  A legal beat line is a strictly increasing sequence of frame
positions ``p[0] < p[1] < ... < p[N-1]`` such that

* every confirmed beat sits exactly on its confirmed frame,
* every gap ``p[i+1] - p[i]`` is inside ``[min_gap, max_gap]``,
* neighbouring gaps differ by at most ``DELTA`` frames (2) — including the
  two gaps on either side of a confirmed beat,

and whose total score ``sum(onset_scores[p[i]])`` is maximal.

The chain structure (unary scores + constraints between neighbouring gaps)
allows an exact forward/backward dynamic program.  A beat position is
*invariant* when it occurs in every optimal line, i.e. when it is the only
position that can be reached with the optimal total score.  A measure may only
receive click cues when all four of its beats are invariant.

Re-solving after a mark change reuses the previous version's tables: the
forward table of beat ``i`` depends only on the marks at beats ``<= i`` and
the backward table only on the marks at beats ``>= i``, so the levels before
the first changed beat and after the last changed beat are identical and can
be shared verbatim.  The recomputed result — total score, candidate
positions, ambiguity — is exactly the same as a full solve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Mapping, Sequence, Tuple

import numpy as np

DELTA = 2
NEG = -(10**7)


class SolverError(Exception):
    """Raised when no legal beat grid exists."""


@dataclass(frozen=True, eq=False)
class SolverState:
    """Reusable dynamic-programming tables of one solved annotation.

    ``forward[i][g][p]`` is the best score of beats ``0..i`` with beat ``i``
    on frame ``p`` and the gap into beat ``i`` equal to ``min_gap + g``.
    ``backward[i][g][p]`` is the best score of beats ``i+1..N-1`` given beat
    ``i`` on frame ``p`` with that same incoming gap.  Index ``0`` of
    ``forward`` and index ``n_beats - 1`` of ``backward`` carry the boundary
    levels (beat 0 has no incoming gap; the last beat's backward table is the
    terminal level).

    Every array is read-only, so successive solves of the same recording may
    share the unchanged prefix/suffix levels without copying.  ``confirmed``
    records the marks the tables were built with, as sorted ``(beat, frame)``
    pairs (0-based beats).
    """

    n_beats: int
    n_frames: int
    min_gap: int
    max_gap: int
    onset_fingerprint: int
    confirmed: Tuple[Tuple[int, int], ...]
    forward: Tuple[Tuple[np.ndarray, ...] | None, ...]
    backward: Tuple[Tuple[np.ndarray, ...] | None, ...]


@dataclass(frozen=True)
class SolveResult:
    """Result of the exact beat-grid search.

    ``candidates[i]`` contains every frame for beat ``i`` that appears in at
    least one optimal legal line.  For confirmed beats this is a singleton.

    The remaining fields describe the reusable state and how much of the
    dynamic program was recomputed; they are bookkeeping, not part of the
    result's identity.  ``changed_beats`` is ``None`` when no reusable state
    was supplied, otherwise the sorted 0-based beats whose mark was added,
    removed or moved relative to the reused state (empty when identical).
    """

    total_score: int
    candidates: Tuple[Tuple[int, ...], ...]
    ambiguous_beats: Tuple[int, ...]
    ambiguous_measures: Tuple[int, ...]
    state: SolverState | None = field(default=None, compare=False, repr=False)
    changed_beats: Tuple[int, ...] | None = field(default=None, compare=False)
    forward_levels_computed: int = field(default=0, compare=False)
    forward_levels_reused: int = field(default=0, compare=False)
    backward_levels_computed: int = field(default=0, compare=False)
    backward_levels_reused: int = field(default=0, compare=False)

    def unique_position(self, beat: int) -> int | None:
        c = self.candidates[beat]
        return c[0] if len(c) == 1 else None


def _mask_to_position(rows: List[np.ndarray], position: int) -> None:
    """Keep only ``position`` in every array of ``rows`` (in place)."""

    kept = [int(row[position]) for row in rows]
    for row, value in zip(rows, kept):
        row.fill(NEG)
        row[position] = value


def _forward_level(
    prev: Sequence[np.ndarray] | None,
    onset: np.ndarray,
    gap_values: Tuple[int, ...],
    gap_to_idx: Mapping[int, int],
    p_first: int,
) -> List[np.ndarray]:
    """One forward level: tables of beat ``i`` from those of beat ``i - 1``.

    ``prev=None`` produces the first level (beat 1), where no earlier gap
    constrains the first interval.  ``prev`` is only read, never mutated, so
    reused tables can be passed directly.
    """

    n_frames = int(onset.size)
    cur = [np.full(n_frames, NEG, dtype=np.int32) for _ in gap_values]
    if prev is None:
        for gi, gap in enumerate(gap_values):
            nxt = p_first + gap
            if nxt < n_frames:
                cur[gi][nxt] = int(onset[p_first]) + int(onset[nxt])
        return cur
    lo_gap, hi_gap = gap_values[0], gap_values[-1]
    for gi, gap in enumerate(gap_values):
        best = cur[gi]
        lo = max(lo_gap, gap - DELTA)
        hi = min(hi_gap, gap + DELTA)
        for prev_gap in range(lo, hi + 1):
            source = prev[gap_to_idx[prev_gap]]
            # source[p] -> target[p + gap]
            vals = source[: n_frames - gap] + onset[gap:]
            np.maximum(best[gap:], vals, out=best[gap:])
    return cur


def _backward_level(
    b_next: Sequence[np.ndarray],
    onset: np.ndarray,
    gap_values: Tuple[int, ...],
    gap_to_idx: Mapping[int, int],
) -> List[np.ndarray]:
    """One backward level: tables of beat ``i`` from those of beat ``i + 1``."""

    n_frames = int(onset.size)
    b_cur = [np.full(n_frames, NEG, dtype=np.int32) for _ in gap_values]
    lo_gap, hi_gap = gap_values[0], gap_values[-1]
    for gi, gap in enumerate(gap_values):
        best = b_cur[gi]
        lo = max(lo_gap, gap - DELTA)
        hi = min(hi_gap, gap + DELTA)
        for next_gap in range(lo, hi + 1):
            source = b_next[gap_to_idx[next_gap]]
            vals = onset[next_gap:] + source[next_gap:]
            np.maximum(best[: n_frames - next_gap], vals, out=best[: n_frames - next_gap])
    return b_cur


def _freeze_tables(
    tables: List[List[np.ndarray] | Tuple[np.ndarray, ...] | None],
) -> Tuple[Tuple[np.ndarray, ...] | None, ...]:
    """Snapshot table lists as tuples of read-only arrays for safe sharing."""

    frozen: List[Tuple[np.ndarray, ...] | None] = []
    for rows in tables:
        if rows is None:
            frozen.append(None)
            continue
        for row in rows:
            row.setflags(write=False)
        frozen.append(tuple(rows))
    return tuple(frozen)


def solve_beat_grid(
    onset_scores: Sequence[int],
    n_beats: int,
    min_gap: int,
    max_gap: int,
    confirmed: Mapping[int, int],
    reuse: SolverState | None = None,
) -> SolveResult:
    """Find the optimal beat grid and per-beat candidate positions.

    Parameters
    ----------
    onset_scores:
        One non-negative integer score per 10 ms frame.
    n_beats:
        Total number of beats (``4 * measures``).
    min_gap, max_gap:
        Allowed distance in frames between two neighbouring beats.
    confirmed:
        Mapping ``beat_index -> frame`` (0-based beat index).  Beat ``0`` and
        beat ``n_beats - 1`` must be confirmed.
    reuse:
        Optional :class:`SolverState` of a previous solve of the *same*
        recording (same frame count, beat count and gap range).  Forward
        levels before the first changed mark and backward levels after the
        last changed mark are reused verbatim; the result is identical to a
        full solve.  An incompatible state is ignored and a full solve is
        performed.
    """

    if n_beats < 2:
        raise ValueError("at least two beats are required")
    if min_gap < 1 or max_gap < min_gap:
        raise ValueError("invalid gap range")
    onset = np.asarray(onset_scores, dtype=np.int32)
    if onset.ndim != 1 or onset.size == 0:
        raise ValueError("onset_scores must be a non-empty 1-D sequence")
    if np.any(onset < 0):
        raise ValueError("onset_scores must be non-negative")

    n_frames = int(onset.size)
    confirmed = {int(beat): int(frame) for beat, frame in confirmed.items()}
    if 0 not in confirmed or (n_beats - 1) not in confirmed:
        raise ValueError("the first and the last beat must be confirmed")

    p_first = confirmed[0]
    p_last = confirmed[n_beats - 1]
    for beat, frame in confirmed.items():
        if not 0 <= beat < n_beats:
            raise ValueError(f"confirmed beat out of range: {beat}")
        if not 0 <= frame < n_frames:
            raise ValueError(f"confirmed frame out of range: {frame}")
    ordered = sorted(confirmed)
    for left, right in zip(ordered, ordered[1:]):
        if confirmed[left] >= confirmed[right]:
            raise ValueError("confirmed frames must increase with beat order")

    gap_values = tuple(range(min_gap, max_gap + 1))
    gap_to_idx = {gap: i for i, gap in enumerate(gap_values)}
    n_gaps = len(gap_values)
    confirmed_set = set(confirmed)

    # ---------------------------------------------------------- reuse plan
    # The forward table of beat i depends only on the marks at beats <= i and
    # the backward table only on the marks at beats >= i, so levels outside
    # the changed beat range [first, last] can be reused as-is.
    state_in: SolverState | None = None
    changed_beats: Tuple[int, ...] | None = None
    fwd_from = 1  # compute forward levels for beats fwd_from .. n_beats-1
    bwd_from = n_beats - 1  # compute backward levels for beats bwd_from .. 1
    if (
        reuse is not None
        and reuse.n_beats == n_beats
        and reuse.n_frames == n_frames
        and reuse.min_gap == min_gap
        and reuse.max_gap == max_gap
        and reuse.onset_fingerprint == hash(onset.tobytes())
    ):
        state_in = reuse
        old = dict(reuse.confirmed)
        changed = sorted(
            beat
            for beat in set(old) | set(confirmed)
            if old.get(beat) != confirmed.get(beat)
        )
        changed_beats = tuple(changed)
        if changed:
            fwd_from = max(changed[0], 1)
            bwd_from = min(changed[-1], n_beats - 1)
        else:
            fwd_from = n_beats  # identical marks: reuse every level
            bwd_from = 0

    # ---------------------------------------------------------------- forward
    f_tables: List[List[np.ndarray] | Tuple[np.ndarray, ...] | None] = [None] * n_beats
    if state_in is not None:
        for beat in range(1, min(fwd_from, n_beats)):
            f_tables[beat] = state_in.forward[beat]

    prev: List[np.ndarray] | Tuple[np.ndarray, ...] | None = None
    if 1 < fwd_from < n_beats:
        prev = f_tables[fwd_from - 1]
    for beat in range(max(1, fwd_from), n_beats):
        cur = _forward_level(prev, onset, gap_values, gap_to_idx, p_first)
        if beat in confirmed_set:
            _mask_to_position(cur, confirmed[beat])
        f_tables[beat] = cur
        prev = cur

    best_last = f_tables[n_beats - 1]
    assert best_last is not None
    total_score = int(max(int(row.max()) for row in best_last))
    if total_score < 0:  # scores are non-negative; a negative value is invalid
        raise SolverError("no legal beat grid satisfies the constraints")

    # --------------------------------------------------------------- backward
    b_tables: List[List[np.ndarray] | Tuple[np.ndarray, ...] | None] = [None] * n_beats
    if state_in is not None:
        for beat in range(max(bwd_from + 1, 1), n_beats):
            b_tables[beat] = state_in.backward[beat]
    if bwd_from >= n_beats - 1:
        # Terminal level: no beats after the last one, score 0 on its frame.
        last_row = np.full(n_frames, NEG, dtype=np.int32)
        last_row[p_last] = 0
        b_tables[n_beats - 1] = [last_row] * n_gaps
    for beat in range(min(bwd_from, n_beats - 2), 0, -1):
        b_cur = _backward_level(b_tables[beat + 1], onset, gap_values, gap_to_idx)
        if beat in confirmed_set:
            _mask_to_position(b_cur, confirmed[beat])
        b_tables[beat] = b_cur

    # ---------------------------------------------------------------- combine
    candidates: List[Tuple[int, ...]] = [tuple() for _ in range(n_beats)]
    candidates[0] = (p_first,)
    candidates[n_beats - 1] = (p_last,)
    for beat in range(1, n_beats - 1):
        if beat in confirmed_set:
            candidates[beat] = (confirmed[beat],)
            continue
        f_table = f_tables[beat]
        b_table = b_tables[beat]
        assert f_table is not None and b_table is not None
        combined = np.full(n_frames, NEG, dtype=np.int32)
        for gi in range(n_gaps):
            np.maximum(combined, f_table[gi] + b_table[gi], out=combined)
        positions = np.flatnonzero(combined == total_score)
        candidates[beat] = tuple(int(p) for p in positions)
        if not candidates[beat]:
            # Every position at this beat would contradict optimality; this
            # cannot happen when the forward/backward tables are consistent.
            raise AssertionError("optimal line reconstruction failed")

    ambiguous_beats = tuple(i for i in range(n_beats) if len(candidates[i]) != 1)
    ambiguous_measures = tuple(sorted({i // 4 + 1 for i in ambiguous_beats}))

    forward_computed = max(0, n_beats - max(1, fwd_from))
    backward_computed = min(bwd_from, n_beats - 1)
    state = SolverState(
        n_beats=n_beats,
        n_frames=n_frames,
        min_gap=min_gap,
        max_gap=max_gap,
        onset_fingerprint=hash(onset.tobytes()),
        confirmed=tuple(sorted(confirmed.items())),
        forward=_freeze_tables(f_tables),
        backward=_freeze_tables(b_tables),
    )
    return SolveResult(
        total_score=total_score,
        candidates=tuple(candidates),
        ambiguous_beats=ambiguous_beats,
        ambiguous_measures=ambiguous_measures,
        state=state,
        changed_beats=changed_beats,
        forward_levels_computed=forward_computed,
        forward_levels_reused=(n_beats - 1) - forward_computed,
        backward_levels_computed=backward_computed,
        backward_levels_reused=(n_beats - 1) - backward_computed,
    )
