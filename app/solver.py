"""Beat-grid solver.

The solver finds every beat position that can occur in *some* highest-scoring
legal beat line.  A legal beat line is a strictly increasing sequence of frame
positions ``p[0] < p[1] < ... < p[N-1]`` such that

* every confirmed beat sits exactly on its confirmed frame,
* every gap ``p[i+1] - p[i]`` is inside ``[min_gap, max_gap]``,
* neighbouring gaps differ by at most ``DELTA`` frames (2),

and whose total score ``sum(onset_scores[p[i]])`` is maximal.

The chain structure (unary scores + constraints between neighbouring gaps)
allows an exact forward/backward dynamic program.  A beat position is
*invariant* when it occurs in every optimal line, i.e. when it is the only
position that can be reached with the optimal total score.  A measure may only
receive click cues when all four of its beats are invariant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping, Sequence, Tuple

import numpy as np

DELTA = 2
NEG = -(10**7)


class SolverError(Exception):
    """Raised when no legal beat grid exists."""


@dataclass(frozen=True)
class SolveResult:
    """Result of the exact beat-grid search.

    ``candidates[i]`` contains every frame for beat ``i`` that appears in at
    least one optimal legal line.  For confirmed beats this is a singleton.
    """

    total_score: int
    candidates: Tuple[Tuple[int, ...], ...]
    ambiguous_beats: Tuple[int, ...]
    ambiguous_measures: Tuple[int, ...]

    def unique_position(self, beat: int) -> int | None:
        c = self.candidates[beat]
        return c[0] if len(c) == 1 else None


def _mask_to_position(rows: List[np.ndarray], position: int) -> None:
    """Keep only ``position`` in every array of ``rows`` (in place)."""

    kept = [int(row[position]) for row in rows]
    for row, value in zip(rows, kept):
        row.fill(NEG)
        row[position] = value


def solve_beat_grid(
    onset_scores: Sequence[int],
    n_beats: int,
    min_gap: int,
    max_gap: int,
    confirmed: Mapping[int, int],
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
    if 0 not in confirmed or (n_beats - 1) not in confirmed:
        raise ValueError("the first and the last beat must be confirmed")

    p_first = int(confirmed[0])
    p_last = int(confirmed[n_beats - 1])
    for beat, frame in confirmed.items():
        if not 0 <= int(beat) < n_beats:
            raise ValueError(f"confirmed beat out of range: {beat}")
        if not 0 <= int(frame) < n_frames:
            raise ValueError(f"confirmed frame out of range: {frame}")
    for beat in sorted(confirmed):
        for other in sorted(confirmed):
            if beat < other and int(confirmed[beat]) >= int(confirmed[other]):
                raise ValueError("confirmed frames must increase with beat order")

    gap_values = tuple(range(min_gap, max_gap + 1))
    gap_to_idx = {gap: i for i, gap in enumerate(gap_values)}
    n_gaps = len(gap_values)
    confirmed_set = {int(b) for b in confirmed}

    # ---------------------------------------------------------------- forward
    # f_tables[i][g][p] = best score for beats 0..i when beat i is at frame p
    # and the gap leading into beat i is ``gap_values[g]``.  Beat 0 has no
    # incoming gap and is handled separately (it is always confirmed).
    # Only tables of unconfirmed beats are retained for the final combination.
    f_tables: List[List[np.ndarray] | None] = [None] * n_beats
    prev: List[np.ndarray] | None = None

    for beat in range(1, n_beats):
        cur = [np.full(n_frames, NEG, dtype=np.int32) for _ in range(n_gaps)]
        if beat == 1:
            # No previous gap constrains the very first interval.
            for gi, gap in enumerate(gap_values):
                nxt = p_first + gap
                if nxt < n_frames:
                    cur[gi][nxt] = int(onset[p_first]) + int(onset[nxt])
        else:
            assert prev is not None
            for gi, gap in enumerate(gap_values):
                best = cur[gi]
                lo = max(min_gap, gap - DELTA)
                hi = min(max_gap, gap + DELTA)
                for prev_gap in range(lo, hi + 1):
                    source = prev[gap_to_idx[prev_gap]]
                    # source[p] -> target[p + gap]
                    vals = source[: n_frames - gap] + onset[gap:]
                    np.maximum(best[gap:], vals, out=best[gap:])
        if beat in confirmed_set:
            _mask_to_position(cur, int(confirmed[beat]))
        if beat not in confirmed_set:
            f_tables[beat] = cur
        prev = cur

    best_last = prev
    assert best_last is not None
    total_score = int(max(int(row.max()) for row in best_last))
    if total_score < 0:  # scores are non-negative; a negative value is invalid
        raise SolverError("no legal beat grid satisfies the constraints")

    # --------------------------------------------------------------- backward
    # b_next[g][p] = best score for beats i+1..N-1 given beat i at frame p and
    # gap g leading into beat i (score of beat i itself excluded).
    last_row = np.full(n_frames, NEG, dtype=np.int32)
    last_row[p_last] = 0
    b_next = [last_row for _ in range(n_gaps)]

    candidates: List[Tuple[int, ...]] = [tuple() for _ in range(n_beats)]
    candidates[0] = (p_first,)
    candidates[n_beats - 1] = (p_last,)

    for beat in range(n_beats - 2, 0, -1):
        b_cur = [np.full(n_frames, NEG, dtype=np.int32) for _ in range(n_gaps)]
        for gi, gap in enumerate(gap_values):
            best = b_cur[gi]
            lo = max(min_gap, gap - DELTA)
            hi = min(max_gap, gap + DELTA)
            for next_gap in range(lo, hi + 1):
                source = b_next[gap_to_idx[next_gap]]
                vals = onset[next_gap:] + source[next_gap:]
                np.maximum(best[: n_frames - next_gap], vals, out=best[: n_frames - next_gap])
        if beat in confirmed_set:
            _mask_to_position(b_cur, int(confirmed[beat]))
            candidates[beat] = (int(confirmed[beat]),)
        else:
            f_table = f_tables[beat]
            assert f_table is not None
            combined = np.full(n_frames, NEG, dtype=np.int32)
            for gi in range(n_gaps):
                np.maximum(combined, f_table[gi] + b_cur[gi], out=combined)
            positions = np.flatnonzero(combined == total_score)
            candidates[beat] = tuple(int(p) for p in positions)
            if not candidates[beat]:
                # Every position at this beat would contradict optimality; this
                # cannot happen when the forward/backward tables are consistent.
                raise AssertionError("optimal line reconstruction failed")
        b_next = b_cur

    ambiguous_beats = tuple(i for i in range(n_beats) if len(candidates[i]) != 1)
    ambiguous_measures = tuple(
        sorted({i // 4 + 1 for i in ambiguous_beats})
    )
    return SolveResult(
        total_score=total_score,
        candidates=tuple(candidates),
        ambiguous_beats=ambiguous_beats,
        ambiguous_measures=ambiguous_measures,
    )
