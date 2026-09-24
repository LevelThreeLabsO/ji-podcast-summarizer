#!/usr/bin/env bash
# TEMPORARY probe #3: measure the real-world success rate of caption fetching from
# a GH Actions IP, using REAL current news videos discovered from YouTube RSS
# (no hand-picked IDs), with the maximal free config. Also tests Invidious/Piped.
# Delete after the run.
set +e

MODE="${MODE:-full}"
echo "##############################################"
echo "# PROBE 3 — MODE: $MODE"
echo "##############################################"
curl -s --max-time 20 https://ipinfo.io/json | grep -E '"ip"|"org"'
echo

python -m pip install -q -U "yt-dlp[default,curl-cffi]" 2>&1 | tail -2
echo -n "yt-dlp version: "; python -m yt_dlp --version
curl -fsSL https://deno.land/install.sh | sh -s -- -y >/dev/null 2>&1
export PATH="$HOME/.deno/bin:$PATH"
echo -n "deno: "; deno --version 2>/dev/null | head -1

python -m pip install -q -U bgutil-ytdlp-pot-provider youtube-transcript-api 2>&1 | tail -2
docker run --name bgutil-provider -d -p 4416:4416 \
  brainicism/bgutil-ytdlp-pot-provider >/dev/null 2>&1
for i in $(seq 1 30); do
  curl -s --max-time 3 http://127.0.0.1:4416/ping >/dev/null 2>&1 && break
  sleep 2
done
echo -n "bgutil provider: "; curl -s --max-time 5 http://127.0.0.1:4416/ping | head -c 200; echo

# ---------------------------------------------------------------------------
# Discover REAL, CURRENT video IDs from YouTube channel RSS feeds.
# RSS is unauthenticated and (per this test) not part of the bot wall, so it
# gives us a genuine sample of the kind of video this bot actually processes:
# news interviews, panels, hearings.
# ---------------------------------------------------------------------------
echo
echo "=============================================="
echo "DISCOVERING REAL VIDEO IDs FROM YOUTUBE RSS"
echo "=============================================="
declare -A CHANNELS=(
  [CNN]=UCupvZG-5ko_eiXAupbDfxWw
  [FoxNews]=UCXIJgqnII2ZOINSWNRGApHA
  [PBSNewsHour]=UC6ZFN9Tx6xh-skXCuRHCDpQ
  [MSNBC]=UCaXkIU1QidjPwiAYu6GcHjg
  [CSPAN]=UCb--64Gl51jIEVE-GLDAVTg
)
: > /tmp/real_ids.txt
for NAME in "${!CHANNELS[@]}"; do
  CID="${CHANNELS[$NAME]}"
  CODE=$(curl -s -o /tmp/rss.xml -w "%{http_code}" --max-time 25 \
    "https://www.youtube.com/feeds/videos.xml?channel_id=$CID")
  IDS=$(grep -oE "<yt:videoId>[^<]+</yt:videoId>" /tmp/rss.xml 2>/dev/null \
        | sed -E 's#</?yt:videoId>##g' | head -2)
  echo "RSS[$NAME]: HTTP $CODE  ids=$(echo $IDS | tr '\n' ' ')"
  for I in $IDS; do echo "$NAME $I" >> /tmp/real_ids.txt; done
done
echo "--- discovered $(wc -l < /tmp/real_ids.txt) real video IDs ---"

cat > /tmp/parse_json3.py <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
n = 0
for e in d.get("events", []):
    if "".join(s.get("utf8", "") for s in (e.get("segs") or [])).strip():
        n += 1
print("   parsed_segments=%d" % n)
PYEOF

PASS=0; FAIL=0; BOTWALL=0
run_caption_test () {
  LABEL="$1"; VID="$2"
  DIR=$(mktemp -d)
  echo
  echo "--- TEST $LABEL ($VID) ---"
  OUT=$(python -m yt_dlp --skip-download --write-subs --write-auto-subs \
    --sub-langs "en,en-orig,en-US,en-GB" --sub-format json3 \
    --no-progress --socket-timeout 30 -o "$DIR/%(id)s" \
    -- "https://www.youtube.com/watch?v=$VID" 2>&1)
  FOUND=$(ls "$DIR"/*.json3 2>/dev/null | head -1)
  if [ -n "$FOUND" ]; then
    echo "RESULT[$LABEL $VID]: SUCCESS bytes=$(stat -c%s "$FOUND")"
    python /tmp/parse_json3.py "$FOUND"
    PASS=$((PASS+1))
  else
    if echo "$OUT" | grep -q "not a bot"; then
      echo "RESULT[$LABEL $VID]: FAILED-BOTWALL"
      BOTWALL=$((BOTWALL+1))
    else
      echo "RESULT[$LABEL $VID]: FAILED-OTHER"
    fi
    echo "$OUT" | grep -E "^ERROR" | head -2 | sed 's/^/    /'
    FAIL=$((FAIL+1))
  fi
  rm -rf "$DIR"
}

echo
echo "=============================================="
echo "REAL NEWS VIDEOS — yt-dlp caption fetch"
echo "=============================================="
while read -r NAME VID; do
  [ -z "$VID" ] && continue
  run_caption_test "$NAME" "$VID"
done < /tmp/real_ids.txt

echo
echo "--- CONTROL: dQw4w9WgXcQ (the one that kept passing in probes 1-2) ---"
run_caption_test "CONTROL" dQw4w9WgXcQ

echo
echo "=============================================================="
echo "yt-dlp TALLY: PASS=$PASS FAIL=$FAIL (of which bot-wall=$BOTWALL)"
echo "=============================================================="

echo
echo "=============================================="
echo "youtube-transcript-api ON THE SAME REAL IDs"
echo "=============================================="
cut -d' ' -f2 /tmp/real_ids.txt > /tmp/ids_only.txt
echo dQw4w9WgXcQ >> /tmp/ids_only.txt
cat > /tmp/yta_test.py <<'PYEOF'
from youtube_transcript_api import YouTubeTranscriptApi
ok = bad = 0
for vid in [l.strip() for l in open("/tmp/ids_only.txt") if l.strip()]:
    try:
        raw = YouTubeTranscriptApi().fetch(vid).to_raw_data()
        print("RESULT[yta %s]: SUCCESS segments=%d" % (vid, len(raw)))
        ok += 1
    except Exception as e:
        print("RESULT[yta %s]: FAILED %s" % (vid, type(e).__name__))
        bad += 1
print("YTA TALLY: PASS=%d FAIL=%d" % (ok, bad))
PYEOF
python /tmp/yta_test.py

echo
echo "=============================================="
echo "INVIDIOUS / PIPED PUBLIC INSTANCES (free proxies)"
echo "=============================================="
TESTID=$(head -1 /tmp/ids_only.txt)
echo "using real video id: $TESTID"
echo "--- fetching current Invidious instance list ---"
curl -s --max-time 25 "https://api.invidious.io/instances.json?sort_by=health" \
  -o /tmp/inv.json
python - <<'PYEOF' > /tmp/inv_hosts.txt 2>/dev/null
import json
try:
    d = json.load(open("/tmp/inv.json"))
    hosts = [x[0] for x in d if x[1].get("api") and x[1].get("type") in ("https",)]
    print("\n".join(hosts[:6]))
except Exception as e:
    pass
PYEOF
echo "instances found: $(wc -l < /tmp/inv_hosts.txt)"
while read -r H; do
  [ -z "$H" ] && continue
  CODE=$(curl -s -o /tmp/iv.out -w "%{http_code}" --max-time 25 \
    "https://$H/api/v1/captions/$TESTID")
  echo "RESULT[invidious $H]: HTTP $CODE bytes=$(stat -c%s /tmp/iv.out 2>/dev/null)"
  head -c 150 /tmp/iv.out; echo
done < /tmp/inv_hosts.txt

for P in pipedapi.kavin.rocks pipedapi.adminforge.de api.piped.private.coffee; do
  CODE=$(curl -s -o /tmp/pp.out -w "%{http_code}" --max-time 25 \
    "https://$P/streams/$TESTID")
  echo "RESULT[piped $P]: HTTP $CODE bytes=$(stat -c%s /tmp/pp.out 2>/dev/null)"
  head -c 150 /tmp/pp.out; echo
done

echo
echo "=============================================="
echo "AUDIO DOWNLOAD on a REAL news video (Whisper path)"
echo "=============================================="
DIR=$(mktemp -d)
python -m yt_dlp -f "bestaudio/best" --no-progress --socket-timeout 30 \
  -o "$DIR/%(id)s.%(ext)s" -- "https://www.youtube.com/watch?v=$TESTID" 2>&1 \
  | grep -vE "^\[download\]\s+[0-9]" | tail -8
ls -la "$DIR" | tail -3
if ls "$DIR"/* >/dev/null 2>&1; then echo "RESULT[audio-real]: SUCCESS"; else echo "RESULT[audio-real]: FAILED"; fi
rm -rf "$DIR"

echo
echo "=============================================="
echo "PROBE 3 COMPLETE"
echo "=============================================="
