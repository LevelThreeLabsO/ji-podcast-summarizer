#!/usr/bin/env bash
# TEMPORARY probe #2: can a JS runtime (deno/node), curl_cffi impersonation, or a
# PO-token provider beat YouTube's datacenter-IP bot wall on GitHub Actions?
# MODE is supplied by the workflow matrix. Delete after the run.
set +e

MODE="${MODE:-baseline}"
echo "##############################################"
echo "# MODE: $MODE"
echo "##############################################"
echo "--- runner public IP / ASN ---"
curl -s --max-time 20 https://ipinfo.io/json | grep -E '"ip"|"org"|"region"'
echo

python -m pip install -q -U yt-dlp 2>&1 | tail -2
echo -n "yt-dlp version: "; python -m yt_dlp --version

EXTRA_GLOBAL=()

case "$MODE" in
  baseline)
    echo "No JS runtime, no impersonation. Long video FIRST (ordering control)."
    ;;
  node)
    echo "Using preinstalled Node as the JS runtime (zero install cost)."
    node --version
    EXTRA_GLOBAL+=(--js-runtimes "node")
    ;;
  deno)
    echo "Installing Deno as the JS runtime."
    curl -fsSL https://deno.land/install.sh | sh -s -- -y >/dev/null 2>&1
    export PATH="$HOME/.deno/bin:$PATH"
    deno --version | head -1
    ;;
  deno_impersonate)
    echo "Deno + curl_cffi impersonation."
    curl -fsSL https://deno.land/install.sh | sh -s -- -y >/dev/null 2>&1
    export PATH="$HOME/.deno/bin:$PATH"
    deno --version | head -1
    python -m pip install -q -U "yt-dlp[default,curl-cffi]" 2>&1 | tail -2
    python -c "from curl_cffi import requests; print('curl_cffi installed OK')" 2>&1 | tail -2
    ;;
  potoken)
    echo "Deno + curl_cffi + bgutil PO-token provider (docker)."
    curl -fsSL https://deno.land/install.sh | sh -s -- -y >/dev/null 2>&1
    export PATH="$HOME/.deno/bin:$PATH"
    deno --version | head -1
    python -m pip install -q -U "yt-dlp[default,curl-cffi]" 2>&1 | tail -2
    python -m pip install -q -U bgutil-ytdlp-pot-provider 2>&1 | tail -2
    echo "--- starting bgutil provider container ---"
    docker run --name bgutil-provider -d -p 4416:4416 \
      brainicism/bgutil-ytdlp-pot-provider 2>&1 | tail -3
    for i in $(seq 1 30); do
      curl -s --max-time 3 http://127.0.0.1:4416/ping >/dev/null 2>&1 && break
      sleep 2
    done
    echo -n "provider /ping: "
    curl -s --max-time 5 http://127.0.0.1:4416/ping | head -c 300; echo
    ;;
esac

echo "EXTRA_GLOBAL args: ${EXTRA_GLOBAL[*]:-<none>}"

cat > /tmp/parse_json3.py <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
n = 0
first = ""
for e in d.get("events", []):
    t = "".join(s.get("utf8", "") for s in (e.get("segs") or [])).strip()
    if t:
        n += 1
        if not first:
            first = t
print("   parsed_segments=%d first_segment=%r" % (n, first[:80]))
PYEOF

run_caption_test () {
  LABEL="$1"; VID="$2"; shift 2
  DIR=$(mktemp -d)
  echo
  echo "======================================================"
  echo "TEST: $LABEL  (video=$VID)  mode=$MODE"
  echo "extra args: $*"
  echo "------------------------------------------------------"
  START=$(date +%s)
  python -m yt_dlp --skip-download --write-subs --write-auto-subs \
    --sub-langs "en,en-orig,en-US,en-GB" --sub-format json3 \
    --no-progress --socket-timeout 30 \
    "${EXTRA_GLOBAL[@]}" "$@" -o "$DIR/%(id)s" \
    -- "https://www.youtube.com/watch?v=$VID" 2>&1 | grep -vE "^\[download\]" | tail -18
  RC=${PIPESTATUS[0]}
  END=$(date +%s)
  echo "--- exit code: $RC   elapsed: $((END-START))s ---"
  FOUND=$(ls "$DIR"/*.json3 2>/dev/null | head -1)
  if [ -n "$FOUND" ]; then
    echo "RESULT[$MODE/$LABEL]: SUCCESS json3=$(basename "$FOUND") bytes=$(stat -c%s "$FOUND")"
    python /tmp/parse_json3.py "$FOUND"
  else
    echo "RESULT[$MODE/$LABEL]: FAILED (no .json3 file)"
  fi
  rm -rf "$DIR"
}

# LONG REAL-WORLD VIDEO FIRST — this is the ordering control. In probe #1 the
# only success was the very first request of the run, so the long videos were
# never tested from a "fresh" runner state.
echo
echo "=============================================="
echo "CAPTION TESTS (long real-world video FIRST)"
echo "=============================================="
run_caption_test "1st-long-bTJggsMK6uQ"  bTJggsMK6uQ
run_caption_test "2nd-long-77y4dn5Dgvs"  77y4dn5Dgvs
run_caption_test "3rd-tiny-jNQXAC9IVRw"  jNQXAC9IVRw
run_caption_test "4th-rickroll-dQw4w9WgXcQ" dQw4w9WgXcQ

if [ "$MODE" = "potoken" ]; then
  echo
  echo "=============================================="
  echo "PO-TOKEN CLIENT VARIANTS"
  echo "=============================================="
  for CLIENT in mweb tv web web_safari; do
    run_caption_test "pot-$CLIENT-bTJggsMK6uQ" bTJggsMK6uQ \
      --extractor-args "youtube:player_client=$CLIENT"
  done
fi

if [ "$MODE" = "deno_impersonate" ]; then
  echo
  echo "=============================================="
  echo "IMPERSONATION TARGET VARIANTS (long video)"
  echo "=============================================="
  python -m yt_dlp --list-impersonate-targets 2>&1 | head -15
  for TGT in chrome safari edge; do
    run_caption_test "imp-$TGT-bTJggsMK6uQ" bTJggsMK6uQ --impersonate "$TGT"
  done
fi

echo
echo "=============================================="
echo "youtube-transcript-api (same runner/IP)"
echo "=============================================="
python -m pip install -q -U youtube-transcript-api 2>&1 | tail -2
cat > /tmp/yta_test.py <<'PYEOF'
import json, os
from youtube_transcript_api import YouTubeTranscriptApi
mode = os.environ.get("MODE", "?")
for vid in ["bTJggsMK6uQ", "77y4dn5Dgvs", "dQw4w9WgXcQ"]:
    try:
        raw = YouTubeTranscriptApi().fetch(vid).to_raw_data()
        print("RESULT[%s/yta %s]: SUCCESS segments=%d first=%s"
              % (mode, vid, len(raw), json.dumps(raw[:1])[:160]))
    except Exception as e:
        print("RESULT[%s/yta %s]: FAILED %s: %s"
              % (mode, vid, type(e).__name__, str(e)[:400].replace("\n", " ")))
PYEOF
python /tmp/yta_test.py

echo
echo "=============================================="
echo "AUDIO DOWNLOAD (no-captions / Whisper path)"
echo "=============================================="
DIR=$(mktemp -d)
START=$(date +%s)
python -m yt_dlp -f "bestaudio/best" --no-progress --socket-timeout 30 \
  "${EXTRA_GLOBAL[@]}" -o "$DIR/%(id)s.%(ext)s" \
  -- "https://www.youtube.com/watch?v=jNQXAC9IVRw" 2>&1 | grep -vE "^\[download\]\s+[0-9]" | tail -12
RC=${PIPESTATUS[0]}
echo "--- audio exit code: $RC elapsed: $(( $(date +%s) - START ))s ---"
ls -la "$DIR" | tail -5
if ls "$DIR"/* >/dev/null 2>&1; then
  echo "RESULT[$MODE/audio-jNQXAC9IVRw]: SUCCESS"
else
  echo "RESULT[$MODE/audio-jNQXAC9IVRw]: FAILED"
fi
rm -rf "$DIR"

echo
echo "=============================================="
echo "PROBE COMPLETE — MODE $MODE"
echo "=============================================="
