"""Request schemas and shared validation constants."""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field, field_validator, model_validator

BEATS_PER_MEASURE = 4
MIN_MEASURES = 1
MAX_MEASURES = 8
MIN_GAP_LIMIT = 20
MAX_GAP_LIMIT = 100
MAX_SCORE = 1000


class ConfirmedBeat(BaseModel):
    """A human-confirmed beat.

    ``beat`` is 1-based (beat 1 is the downbeat of measure 1); ``frame`` is the
    0-based index of the 10 ms onset frame.
    """

    beat: int = Field(ge=1, le=MAX_MEASURES * BEATS_PER_MEASURE)
    frame: int = Field(ge=0, description="0-based 10 ms frame index")


class RecordingSpec(BaseModel):
    measure_count: int = Field(ge=MIN_MEASURES, le=MAX_MEASURES)
    min_gap_frames: int = Field(ge=MIN_GAP_LIMIT, le=MAX_GAP_LIMIT)
    max_gap_frames: int = Field(ge=MIN_GAP_LIMIT, le=MAX_GAP_LIMIT)
    confirmed_beats: List[ConfirmedBeat] = Field(min_length=1)
    onset_scores: List[int] = Field(min_length=1)

    @field_validator("onset_scores")
    @classmethod
    def scores_in_range(cls, value: List[int]) -> List[int]:
        if any(not 0 <= int(v) <= MAX_SCORE for v in value):
            raise ValueError(f"onset scores must be integers in [0, {MAX_SCORE}]")
        return [int(v) for v in value]

    @model_validator(mode="after")
    def check_consistency(self) -> "RecordingSpec":
        if self.min_gap_frames > self.max_gap_frames:
            raise ValueError("min_gap_frames must not exceed max_gap_frames")
        n_beats = self.measure_count * BEATS_PER_MEASURE
        beats = [c.beat for c in self.confirmed_beats]
        if any(beat > n_beats for beat in beats):
            raise ValueError(
                f"confirmed beat numbers must be within 1..{n_beats} "
                f"for {self.measure_count} measure(s)"
            )
        if len(set(beats)) != len(beats):
            raise ValueError("confirmed beat numbers must be unique")
        pairs = sorted((c.beat, c.frame) for c in self.confirmed_beats)
        if pairs[0][0] != 1:
            raise ValueError("the first beat (beat 1) must be confirmed")
        if pairs[-1][0] != n_beats:
            raise ValueError(f"the last beat (beat {n_beats}) must be confirmed")
        if any(pairs[i][1] >= pairs[i + 1][1] for i in range(len(pairs) - 1)):
            raise ValueError("confirmed frames must increase with beat order")
        return self


class ReannotationRequest(BaseModel):
    """A new set of manual beat marks for an existing recording."""

    confirmed_beats: List[ConfirmedBeat] = Field(min_length=1)

    @model_validator(mode="after")
    def check_consistency(self) -> "ReannotationRequest":
        beats = [c.beat for c in self.confirmed_beats]
        if len(set(beats)) != len(beats):
            raise ValueError("confirmed beat numbers must be unique")
        pairs = sorted((c.beat, c.frame) for c in self.confirmed_beats)
        if any(pairs[i][1] >= pairs[i + 1][1] for i in range(len(pairs) - 1)):
            raise ValueError("confirmed frames must increase with beat order")
        return self
