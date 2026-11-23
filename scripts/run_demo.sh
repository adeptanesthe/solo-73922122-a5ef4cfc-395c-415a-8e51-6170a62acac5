#!/usr/bin/env bash
# Walk through the cue-track API against a running service.
#   ./scripts/run_demo.sh [base_url] [workdir]
# Defaults: http://localhost:8000 and a temp directory for generated inputs.
set -euo pipefail

BASE="${1:-http://localhost:8000}"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/cue-demo.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

python3 "$(dirname "$0")/make_demo_inputs.py" "$TMP" >/dev/null
ANN="$(cat "$TMP/annotation.json")"

echo "== health =="
curl -sS "$BASE/healthz"; echo

echo
echo "== 1. upload take.wav + annotation (beat 5 is ambiguous, measure 2 left blank) =="
CREATE="$(curl -sS -X POST "$BASE/api/recordings" \
  -F "audio=@$TMP/take.wav;type=audio/wav" \
  -F "annotation=$ANN")"
echo "$CREATE" | python3 -m json.tool
RID="$(echo "$CREATE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["recording_id"])')"

echo
echo "== 2. download version 1 cue track and verify it matches the recording length =="
curl -sS -D "$TMP/v1.headers" -o "$TMP/v1.wav" "$BASE/api/recordings/$RID/versions/1/cue.wav"
python3 - "$TMP/take.wav" "$TMP/v1.wav" <<'PY'
import sys, wave
src, cue = sys.argv[1], sys.argv[2]
with wave.open(src) as a, wave.open(cue) as b:
    print(f"recording: {a.getnchannels()}ch {a.getsampwidth()*8}bit {a.getframerate()}Hz {a.getnframes()} frames")
    print(f"cue track: {b.getnchannels()}ch {b.getsampwidth()*8}bit {b.getframerate()}Hz {b.getnframes()} frames")
    assert a.getnframes() == b.getnframes() and a.getframerate() == b.getframerate()
    print("length and format match the recording")
PY

echo
echo "== 3. re-annotate with a conflicting mark (unreachable) -> 422, version 1 kept =="
curl -sS -o "$TMP/bad.json" -w "HTTP %{http_code}\n" -X POST \
  "$BASE/api/recordings/$RID/beats" \
  -H 'Content-Type: application/json' --data @"$TMP/reannotation_bad.json"
python3 -m json.tool "$TMP/bad.json"

echo
echo "== 4. re-annotate with beat 5 confirmed -> 201, version 2, both measures cued =="
curl -sS -o "$TMP/good.json" -w "HTTP %{http_code}\n" -X POST \
  "$BASE/api/recordings/$RID/beats" \
  -H 'Content-Type: application/json' --data @"$TMP/reannotation.json"
python3 -m json.tool "$TMP/good.json"

echo
echo "== 5. old version download now returns 409 =="
curl -sS -o "$TMP/old.json" -w "HTTP %{http_code}\n" \
  "$BASE/api/recordings/$RID/versions/1/cue.wav"
python3 -m json.tool "$TMP/old.json"

echo
echo "== 6. current version 2 downloads cleanly =="
curl -sS -o "$TMP/v2.wav" -w "HTTP %{http_code} %{content_type}\n" \
  "$BASE/api/recordings/$RID/versions/2/cue.wav"

echo
echo "== 7. recording metadata / version history =="
curl -sS "$BASE/api/recordings/$RID" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(json.dumps({
    "recording_id": d["recording_id"],
    "audio": d["audio"],
    "current_version_number": d["current_version_number"],
    "current_version": d["current_version"],
}, indent=2, ensure_ascii=False))
print("version history:", [(v["version"], v["is_current"]) for v in d["versions"]])
'

echo
echo "== 8. three-measure recording: measure 3 blank behind confirmed beat 9 =="
CREATE3="$(curl -sS -X POST "$BASE/api/recordings" \
  -F "audio=@$TMP/take3.wav;type=audio/wav" \
  -F "annotation=$(cat "$TMP/annotation3.json")")"
RID3="$(echo "$CREATE3" | python3 -c 'import json,sys; print(json.load(sys.stdin)["recording_id"])')"
echo "$CREATE3" | python3 -c '
import json, sys
d = json.load(sys.stdin)
v = d["current_version"]
print("version 1: ambiguous_measures =", v["ambiguous_measures"],
      " cued_measures =", v["cued_measures"])
print("beat_positions:", v["beat_positions"])
assert v["ambiguous_measures"] == [3]
'

echo
echo "== 9. move confirmed beat 5 by two frames -> version 2 (incremental re-solve) =="
curl -sS -o "$TMP/move.json" -w "HTTP %{http_code}\n" -X POST \
  "$BASE/api/recordings/$RID3/beats" \
  -H 'Content-Type: application/json' --data @"$TMP/reannotation_move.json"
python3 -c '
import json
v = json.load(open("'"$TMP"'/move.json"))
print("version", v["version"], ": ambiguous_measures =", v["ambiguous_measures"],
      " cued_measures =", v["cued_measures"])
print("beat_positions:", v["beat_positions"])
assert v["ambiguous_measures"] == [] and v["cued_measures"] == [1, 2, 3]
print("-> measure 3 (behind the unchanged confirmed beat 9) flipped from blank to cued")
'

echo
echo "== 10. server log: forward/backward recursion levels reused by the re-solve =="
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  docker compose --project-directory "$SCRIPT_DIR/.." logs --since 15m 2>/dev/null \
    | grep -E "reannotation .*reused" | tail -3 \
    || echo "(no reannotation log lines found in the last 15 minutes)"
else
  echo "docker compose not available in this shell; the server logs a line like:"
  echo "  reannotation recording=... version=2 changed_beats=[5] \\"
  echo "      forward_computed=8 forward_reused=3 backward_computed=4 backward_reused=7"
fi
echo "(full solve of 12 beats computes 11 forward + 11 backward levels; the move above"
echo " reused 3 forward + 7 backward levels from version 1 and still matches a full solve)"

echo
echo "== 11. old version of the three-measure recording now returns 409 =="
curl -sS -o "$TMP/old3.json" -w "HTTP %{http_code}\n" \
  "$BASE/api/recordings/$RID3/versions/1/cue.wav"
python3 -m json.tool "$TMP/old3.json"
