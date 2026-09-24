#!/usr/bin/env python3
"""TEMPORARY probe #4: can Gemini ingest a YouTube URL directly from a GitHub
Actions datacenter IP, and does it produce timestamped "Notable Moments"?

This sidesteps the datacenter-IP bot wall entirely: Google fetches the video
server-side from Google's own infrastructure, so our runner's IP never touches
YouTube's video pipeline. Delete after the run.
"""
import json
import os
import re
import sys
import time
import urllib.request

KEY = os.environ.get("GEMINI_API_KEY") or ""
if not KEY:
    print("FATAL: GEMINI_API_KEY not set")
    sys.exit(0)

MODELS = ["gemini-3.6-flash", "gemini-3-flash-preview", "gemini-flash-latest"]

PROMPT = (
    "You are given a video. List the 3 most notable moments as JSON only:\n"
    '[{"timestamp":"MM:SS","quote":"<verbatim quote>","why":"<one line>"}]\n'
    "Use real timestamps from the video. No prose outside the JSON."
)


def call(model, youtube_url, timeout=600):
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    body = {
        "contents": [{
            "parts": [
                {"file_data": {"file_uri": youtube_url}},
                {"text": PROMPT},
            ]
        }],
        "generationConfig": {"maxOutputTokens": 2048, "temperature": 0},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": KEY},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode())
        return r.status, payload, time.time() - t0, None
    except urllib.error.HTTPError as e:
        return e.code, None, time.time() - t0, e.read().decode()[:600]
    except Exception as e:
        return None, None, time.time() - t0, "%s: %s" % (type(e).__name__, str(e)[:400])


def extract_text(payload):
    try:
        cands = payload.get("candidates") or []
        parts = cands[0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except Exception:
        return ""


def usage(payload):
    u = payload.get("usageMetadata") or {}
    return "prompt_tokens=%s total_tokens=%s" % (
        u.get("promptTokenCount"), u.get("totalTokenCount"))


def run(label, youtube_url, model):
    print()
    print("=" * 62)
    print("TEST %s  model=%s" % (label, model))
    print("url: %s" % youtube_url)
    print("-" * 62)
    status, payload, elapsed, err = call(model, youtube_url)
    print("HTTP %s   elapsed=%.1fs" % (status, elapsed))
    if err:
        print("RESULT[%s/%s]: FAILED" % (model, label))
        print("  error: %s" % err.replace("\n", " ")[:500])
        return False, elapsed
    text = extract_text(payload)
    fr = (payload.get("candidates") or [{}])[0].get("finishReason")
    print("  finishReason=%s  %s" % (fr, usage(payload)))
    if not text.strip():
        print("RESULT[%s/%s]: FAILED (empty response)" % (model, label))
        print("  raw: %s" % json.dumps(payload)[:400])
        return False, elapsed
    print("  response (first 700 chars):")
    print("    " + text.strip()[:700].replace("\n", "\n    "))
    ts = re.findall(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", text)
    print("RESULT[%s/%s]: SUCCESS timestamps_found=%d %s"
          % (model, label, len(ts), ts[:6]))
    return True, elapsed


def main():
    print("#" * 62)
    print("# PROBE 4 — Gemini native YouTube ingestion from a GH Actions IP")
    print("#" * 62)

    # Real, current news videos discovered from YouTube RSS (no hand-picked IDs),
    # plus a known-long video and the control.
    ids = []
    for name, cid in [
        ("CNN", "UCupvZG-5ko_eiXAupbDfxWw"),
        ("PBSNewsHour", "UC6ZFN9Tx6xh-skXCuRHCDpQ"),
        ("CSPAN", "UCb--64Gl51jIEVE-GLDAVTg"),
    ]:
        try:
            with urllib.request.urlopen(
                "https://www.youtube.com/feeds/videos.xml?channel_id=" + cid,
                timeout=25,
            ) as r:
                xml = r.read().decode("utf-8", "replace")
            found = re.findall(r"<yt:videoId>([^<]+)</yt:videoId>", xml)[:1]
            for v in found:
                ids.append((name, v))
            print("RSS[%s]: %s" % (name, found))
        except Exception as e:
            print("RSS[%s]: FAILED %s" % (name, type(e).__name__))

    ids.append(("LONG-2h-ish", "bTJggsMK6uQ"))
    ids.append(("LONG-2", "77y4dn5Dgvs"))
    ids.append(("CONTROL", "dQw4w9WgXcQ"))

    model = MODELS[0]
    passed = failed = 0
    times = []
    for label, vid in ids:
        ok, el = run(label, "https://www.youtube.com/watch?v=%s" % vid, model)
        times.append((label, vid, el, ok))
        if ok:
            passed += 1
        else:
            failed += 1
        time.sleep(3)  # be polite to the free-tier RPM limit

    print()
    print("=" * 62)
    print("GEMINI TALLY (model=%s): PASS=%d FAIL=%d" % (model, passed, failed))
    for label, vid, el, ok in times:
        print("   %-14s %-12s %6.1fs  %s"
              % (label, vid, el, "OK" if ok else "FAIL"))
    print("=" * 62)

    # Fallback-model spot check on one real video, to confirm the chain works.
    if ids:
        print()
        print("--- fallback-model spot check ---")
        for m in MODELS[1:]:
            run("fallback-check", "https://www.youtube.com/watch?v=%s" % ids[0][1], m)
            time.sleep(3)


if __name__ == "__main__":
    main()
