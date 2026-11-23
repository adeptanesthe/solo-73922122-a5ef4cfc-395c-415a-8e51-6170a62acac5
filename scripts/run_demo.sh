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
