"""In-memory recording/version store.

All public methods take the store lock, so concurrent uploads and
re-annotations of the same recording cannot interleave and produce duplicate
version numbers or tear a payload.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Tuple

from .solver import SolveResult
from .wav_io import AudioInfo


@dataclass(frozen=True)
class ConfirmedMark:
    beat: int
    frame: int


@dataclass
class CueVersion:
    number: int
    total_score: int
    beat_positions: List[int | None]
    ambiguous_measures: List[int]
    cued_measures: List[int]
    confirmed_marks: List[ConfirmedMark]
    wav: bytes
    created_at: str
    superseded: bool = False


@dataclass
class Recording:
    id: str
    audio: AudioInfo
    onset_scores: List[int]
    measure_count: int
    min_gap_frames: int
    max_gap_frames: int
    confirmed_marks: List[ConfirmedMark]
    created_at: str
    versions: List[CueVersion] = field(default_factory=list)

    @property
    def current_version(self) -> CueVersion | None:
        for version in reversed(self.versions):
            if not version.superseded:
                return version
        return None


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class Store:
    """Thread-safe, process-lifetime storage."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._recordings: Dict[str, Recording] = {}

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ----------------------------------------------------------- mutations

    def create(
        self,
        audio: AudioInfo,
        onset_scores: List[int],
        measure_count: int,
        min_gap_frames: int,
        max_gap_frames: int,
        confirmed_marks: List[ConfirmedMark],
        result: SolveResult,
        wav: bytes,
    ) -> Recording:
        recording = Recording(
            id=uuid.uuid4().hex,
            audio=audio,
            onset_scores=list(onset_scores),
            measure_count=measure_count,
            min_gap_frames=min_gap_frames,
            max_gap_frames=max_gap_frames,
            confirmed_marks=list(confirmed_marks),
            created_at=self._now(),
        )
        with self.lock:
            self._append_version(recording, result, confirmed_marks, wav)
            self._recordings[recording.id] = recording
        return recording

    def _append_version(
        self,
        recording: Recording,
        result: SolveResult,
        confirmed_marks: List[ConfirmedMark],
        wav: bytes,
    ) -> CueVersion:
        for old in recording.versions:
            old.superseded = True
        positions = [
            result.unique_position(i) for i in range(len(result.candidates))
        ]
        cued = [
            m
            for m in range(1, recording.measure_count + 1)
            if m not in result.ambiguous_measures
        ]
        version = CueVersion(
            number=len(recording.versions) + 1,
            total_score=result.total_score,
            beat_positions=positions,
            ambiguous_measures=list(result.ambiguous_measures),
            cued_measures=cued,
            confirmed_marks=list(confirmed_marks),
            wav=wav,
            created_at=self._now(),
        )
        recording.versions.append(version)
        recording.confirmed_marks = list(confirmed_marks)
        return version

    def reannotate(
        self,
        recording_id: str,
        confirmed_marks: List[ConfirmedMark],
        build: Callable[[Recording], Tuple[SolveResult, bytes]],
    ) -> Tuple[Recording, CueVersion]:
        """Solve and append a version atomically.

        ``build`` performs the (possibly slow) solve/render.  If it raises,
        the current version is untouched and the exception propagates.
        """

        with self.lock:
            recording = self._recordings.get(recording_id)
            if recording is None:
                raise NotFoundError(f"recording {recording_id!r} not found")
            result, wav = build(recording)
            version = self._append_version(recording, result, confirmed_marks, wav)
            return recording, version

    # ------------------------------------------------------------ queries

    def get(self, recording_id: str) -> Recording:
        with self.lock:
            recording = self._recordings.get(recording_id)
        if recording is None:
            raise NotFoundError(f"recording {recording_id!r} not found")
        return recording

    def snapshot(self, recording_id: str) -> dict:
        """Consistent copy of recording metadata and version summaries."""

        with self.lock:
            recording = self._recordings.get(recording_id)
            if recording is None:
                raise NotFoundError(f"recording {recording_id!r} not found")
            current = recording.current_version
            versions = [
                {
                    "version": v.number,
                    "is_current": not v.superseded,
                    "total_score": v.total_score,
                    "beat_positions": list(v.beat_positions),
                    "ambiguous_measures": list(v.ambiguous_measures),
                    "cued_measures": list(v.cued_measures),
                    "confirmed_beats": [
                        {"beat": m.beat, "frame": m.frame} for m in v.confirmed_marks
                    ],
                    "created_at": v.created_at,
                    "download_url": (
                        f"/api/recordings/{recording.id}/versions/"
                        f"{v.number}/cue.wav"
                    ),
                }
                for v in recording.versions
            ]
            current_summary = next(
                (v for v in versions if v["version"] == (current.number if current else None)),
                None,
            )
            return {
                "recording_id": recording.id,
                "audio": {
                    "sample_rate": 16_000,
                    "channels": 1,
                    "bit_depth": 16,
                    "sample_count": int(recording.audio.samples.size),
                    "duration_seconds": recording.audio.duration_seconds,
                    "onset_frames": recording.audio.n_frames,
                },
                "measure_count": recording.measure_count,
                "min_gap_frames": recording.min_gap_frames,
                "max_gap_frames": recording.max_gap_frames,
                "current_version_number": current.number if current else None,
                "current_version": current_summary,
                "versions": versions,
            }

    def current_version_number(self, recording_id: str) -> int | None:
        with self.lock:
            recording = self._recordings.get(recording_id)
            if recording is None:
                raise NotFoundError(f"recording {recording_id!r} not found")
            current = recording.current_version
            return current.number if current else None

    def get_version(
        self, recording_id: str, version_number: int
    ) -> Tuple[CueVersion, int | None]:
        """Return a version plus the current version number (under lock)."""

        with self.lock:
            recording = self._recordings.get(recording_id)
            if recording is None:
                raise NotFoundError(f"recording {recording_id!r} not found")
            for version in recording.versions:
                if version.number == version_number:
                    current = recording.current_version
                    return version, current.number if current else None
        raise NotFoundError(
            f"version {version_number} of recording {recording_id!r} not found"
        )
