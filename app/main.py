"""FastAPI application for choir-rehearsal cue-track annotation."""

from __future__ import annotations

import io
import json
import logging
import wave

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .cue import render_cue_track
from .models import BEATS_PER_MEASURE, RecordingSpec, ReannotationRequest
from .solver import SolverError, solve_beat_grid
from .store import ConfirmedMark, NotFoundError, Store
from .wav_io import SAMPLE_RATE, AudioValidationError, read_wav

MAX_UPLOAD_BYTES = 4 * 1024 * 1024

# Dedicated logger for re-solve reuse statistics; visible via
# ``docker compose logs`` when running the container.
logger = logging.getLogger("cue")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

app = FastAPI(
    title="Choir Rehearsal Cue Track",
    version="1.0.0",
    description=(
        "Annotate a choir rehearsal recording with a stackable click track. "
        "Accepts a 16 kHz / 16-bit / mono PCM WAV of at most 30 s plus one "
        "onset score (0..1000) per 10 ms frame.  Beats must pass through the "
        "confirmed manual marks, stay inside the requested spacing range and "
        "change spacing by at most 2 frames between neighbours.  A measure is "
        "given clicks only when all four of its beats land on the same frame "
        "in every maximum-score beat line; otherwise the whole measure is "
        "left silent and its number is reported as ambiguous."
    ),
)
app.state.store = Store()


def get_store(request: Request) -> Store:
    return request.app.state.store


# --------------------------------------------------------------------- helpers


def _http(code: int, kind: str, message: str, **extra) -> HTTPException:
    detail = {"code": kind, "message": message}
    detail.update(extra)
    return HTTPException(status_code=code, detail=detail)


def _parse_json(raw: str, field_name: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _http(
            422, "invalid_field", f"{field_name} must be valid JSON: {exc.msg}"
        ) from exc


def _validation_error(exc: ValidationError) -> HTTPException:
    return _http(
        422,
        "invalid_annotation",
        "annotation is invalid",
        errors=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
    )


def _check_frame_bounds(marks: list[ConfirmedMark], n_beats: int, n_frames: int) -> None:
    for mark in marks:
        if not 1 <= mark.beat <= n_beats:
            raise _http(
                422,
                "invalid_annotation",
                f"beat {mark.beat} outside 1..{n_beats} "
                f"({n_beats // BEATS_PER_MEASURE} measures requested)",
            )
        if not 0 <= mark.frame < n_frames:
            raise _http(
                422,
                "invalid_annotation",
                f"frame {mark.frame} outside 0..{n_frames - 1} "
                f"for a recording with {n_frames} frames",
            )


def _require_endpoints(marks: list[ConfirmedMark], n_beats: int) -> None:
    beats = {m.beat for m in marks}
    if 1 not in beats or n_beats not in beats:
        raise _http(
            422,
            "missing_endpoint_marks",
            f"beat 1 and beat {n_beats} must both be confirmed",
        )


def _solve_or_422(
    onset_scores: list[int],
    measure_count: int,
    min_gap: int,
    max_gap: int,
    marks: list[ConfirmedMark],
    n_frames: int,
    reuse=None,
):
    n_beats = measure_count * BEATS_PER_MEASURE
    _check_frame_bounds(marks, n_beats, n_frames)
    _require_endpoints(marks, n_beats)
    try:
        return solve_beat_grid(
            onset_scores,
            n_beats,
            min_gap,
            max_gap,
            {m.beat - 1: m.frame for m in marks},
            reuse=reuse,
        )
    except SolverError as exc:
        raise _http(
            422,
            "no_legal_beat_grid",
            "no legal beat line satisfies the confirmed marks, the spacing "
            "range and the ±2-frame neighbour limit",
        ) from exc
    except ValueError as exc:
        raise _http(422, "invalid_annotation", str(exc)) from exc


def _render_cue(sample_count: int, measure_count: int, result) -> bytes:
    positions = [result.unique_position(i) for i in range(len(result.candidates))]
    cued = set(range(1, measure_count + 1)) - set(result.ambiguous_measures)
    track = render_cue_track(sample_count, positions, cued)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(track.tobytes())
    return buffer.getvalue()


# ---------------------------------------------------------------------- routes


@app.get("/healthz", summary="Liveness probe")
def healthz():
    return {"status": "ok"}


@app.post(
    "/api/recordings",
    status_code=201,
    summary="Upload a recording and create its first cue-track version",
)
async def create_recording(
    request: Request,
    audio: UploadFile = File(..., description="16 kHz, 16-bit, mono PCM WAV, ≤ 30 s"),
    annotation: str = Form(
        ...,
        description=(
            'JSON object: {"measure_count": 2, "min_gap_frames": 40, '
            '"max_gap_frames": 46, "confirmed_beats": [{"beat": 1, "frame": 12}], '
            '"onset_scores": [0, 12, ...]} (one score per 10 ms frame)'
        ),
    ),
):
    store = get_store(request)

    data = await audio.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise _http(
            413, "file_too_large", f"WAV upload must be at most {MAX_UPLOAD_BYTES} bytes"
        )
    try:
        info = read_wav(data)
    except AudioValidationError as exc:
        raise _http(422, "invalid_audio", str(exc)) from exc

    raw = _parse_json(annotation, "annotation")
    if not isinstance(raw, dict):
        raise _http(422, "invalid_field", "annotation must be a JSON object")
    try:
        spec = RecordingSpec(**raw)
    except ValidationError as exc:
        raise _validation_error(exc) from exc

    if len(spec.onset_scores) != info.n_frames:
        raise _http(
            422,
            "frame_count_mismatch",
            f"expected {info.n_frames} onset scores for a "
            f"{info.duration_seconds:.3f} s recording, got {len(spec.onset_scores)}",
        )

    marks = [ConfirmedMark(beat=c.beat, frame=c.frame) for c in spec.confirmed_beats]
    result = _solve_or_422(
        spec.onset_scores,
        spec.measure_count,
        spec.min_gap_frames,
        spec.max_gap_frames,
        marks,
        info.n_frames,
    )
    wav = _render_cue(len(info.samples), spec.measure_count, result)
    recording = store.create(
        audio=info,
        onset_scores=spec.onset_scores,
        measure_count=spec.measure_count,
        min_gap_frames=spec.min_gap_frames,
        max_gap_frames=spec.max_gap_frames,
        confirmed_marks=marks,
        result=result,
        wav=wav,
    )
    return JSONResponse(status_code=201, content=store.snapshot(recording.id))


@app.post(
    "/api/recordings/{recording_id}/beats",
    status_code=201,
    summary="Re-submit manual beat marks; replace the current version on success",
)
def reannotate(
    recording_id: str,
    payload: ReannotationRequest,
    request: Request,
):
    store = get_store(request)
    try:
        recording = store.get(recording_id)
    except NotFoundError as exc:
        raise _http(404, "recording_not_found", str(exc)) from exc

    marks = [ConfirmedMark(beat=c.beat, frame=c.frame) for c in payload.confirmed_beats]
    solved: dict = {}

    def build(recording, prev_state):
        # Raises HTTPException on failure; the store keeps the old version
        # because nothing is mutated until this function returns.  The store
        # calls this without holding its lock, so the current version stays
        # queryable and downloadable while the re-solve runs.  ``prev_state``
        # carries the current version's reusable DP tables.
        result = _solve_or_422(
            recording.onset_scores,
            recording.measure_count,
            recording.min_gap_frames,
            recording.max_gap_frames,
            marks,
            recording.audio.n_frames,
            reuse=prev_state,
        )
        solved["result"] = result
        return result, _render_cue(
            len(recording.audio.samples), recording.measure_count, result
        )

    try:
        recording, version = store.reannotate(recording_id, marks, build)
    except NotFoundError as exc:  # pragma: no cover - race with deletion
        raise _http(404, "recording_not_found", str(exc)) from exc
    except HTTPException as exc:
        # No version was appended, so the previously current version stands.
        current = store.current_version_number(recording_id)
        detail = dict(exc.detail) if isinstance(exc.detail, dict) else {}
        detail["message"] = (
            f"{detail.get('message', 'annotation failed')}; "
            f"current version {current if current is not None else '?'} was kept"
        )
        detail["current_version"] = current
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc

    result = solved["result"]
    changed = (
        [beat + 1 for beat in result.changed_beats]
        if result.changed_beats is not None
        else "full-solve"
    )
    logger.info(
        "reannotation recording=%s version=%d changed_beats=%s "
        "forward_computed=%d forward_reused=%d "
        "backward_computed=%d backward_reused=%d",
        recording_id,
        version.number,
        changed,
        result.forward_levels_computed,
        result.forward_levels_reused,
        result.backward_levels_computed,
        result.backward_levels_reused,
    )

    snapshot = store.snapshot(recording_id)
    body = next(v for v in snapshot["versions"] if v["version"] == version.number)
    body = dict(body)
    body.update(
        {
            "recording_id": recording_id,
            "measure_count": recording.measure_count,
            "min_gap_frames": recording.min_gap_frames,
            "max_gap_frames": recording.max_gap_frames,
        }
    )
    return JSONResponse(status_code=201, content=body)


@app.get(
    "/api/recordings/{recording_id}",
    summary="Recording metadata, current version and version history",
)
def get_recording(recording_id: str, request: Request):
    store = get_store(request)
    try:
        return store.snapshot(recording_id)
    except NotFoundError as exc:
        raise _http(404, "recording_not_found", str(exc)) from exc


@app.get(
    "/api/recordings/{recording_id}/versions/{version_number}/cue.wav",
    summary="Download the cue track of an annotation version",
    response_class=Response,
    responses={
        200: {"content": {"audio/wav": {}}, "description": "Cue-track WAV"},
        409: {"description": "Version superseded by a newer annotation"},
    },
)
def download_cue(recording_id: str, version_number: int, request: Request):
    store = get_store(request)
    try:
        version, current_number = store.get_version(recording_id, version_number)
    except NotFoundError as exc:
        raise _http(404, "version_not_found", str(exc)) from exc
    if version.superseded:
        raise _http(
            409,
            "version_superseded",
            f"version {version.number} was superseded by version "
            f"{current_number}; download the current version",
            current_version=current_number,
        )
    filename = f"cue_{recording_id[:8]}_v{version.number}.wav"
    return Response(
        content=version.wav,
        media_type="audio/wav",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Cue-Version": str(version.number),
        },
    )
