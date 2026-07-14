#!/usr/bin/env python3
"""Watches a Slack channel for YouTube URLs and posts a Notable Moments
threaded reply, tuned to Jewish Insider's news-making priorities.

Single-shot: one GH Actions tick = one run. State (last-checked timestamp +
processed message IDs) is committed back to the repo so runs don't re-process.

Usage:
  python poll.py                # real run (needs all env vars)
  python poll.py --dry-run      # process + print, don't post to Slack
  python poll.py --url <URL>    # test a single URL end-to-end (dry-run)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

STATE_FILE = Path(__file__).resolve().parent / "watcher_state.json"

YOUTUBE_RE = re.compile(
    r"https?://(?:www\.|m\.)?"
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/live/|youtube\.com/shorts/)"
    r"([A-Za-z0-9_-]{11})"
)
PODCAST_RE = re.compile(
    r"https?://(?:open\.spotify\.com/episode|podcasts\.apple\.com)/[^\s>|]+"
)
TWEET_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?"
    r"(?:twitter\.com|x\.com)/[^/\s]+/status/(\d+)"
)

SUMMARY_PROMPT = """You are analyzing a video transcript for a reporter at Jewish Insider, \
a publication covering Jewish, Israel, and Middle East policy, politics, and community affairs. \
Your job is to surface the NOTABLE MOMENTS a JI editor would flag in Slack.

What counts as notable:
  - Specific policy claims, commitments, or reversals
  - Controversial or news-making statements on Israel, Iran, Gaza, Hamas, Hezbollah, \
antisemitism, U.S.-Israel relations, Congress, the administration, or upcoming races
  - Quotable pull-quotes that stand on their own
  - Scoops, revelations, first-time-said remarks

What is NOT notable (skip):
  - Pleasantries, thanks, generic opening/closing remarks
  - Well-known positions restated in a routine way
  - Process talk (\"we'll get to that later\", scheduling)

Video: {title}

TRANSCRIPT (with [MM:SS] timestamps):
{transcript}

Return a JSON array of 3-6 items. Each item:
{{
  "headline": "news-headline-style one-liner, action/claim-focused, 15 words max, no filler",
  "start_min": <int minute>,
  "end_min": <int minute>,
  "quote": "a direct verbatim pull-quote from the transcript, under 200 chars, or empty string if no clean one exists"
}}

Rules:
  - start_min/end_min: the rough minute-range the moment spans. If a single point, they're equal.
  - headline uses active voice and specific claims. Bracketed clarifier OK if needed \
(e.g. "(related to Platner)").
  - quote must appear verbatim in the transcript. Do not paraphrase in the quote field.
  - If there are truly no notable moments, return [].
  - Return ONLY the JSON array. No markdown fences, no explanation.
"""


# ── Slack ──────────────────────────────────────────────────────────────────────

class Slack:
    def __init__(self):
        self.token = os.environ.get("SLACK_BOT_TOKEN")
        self.channel = os.environ.get("SLACK_CHANNEL_ID")

    def _call(self, method, params=None, json_body=None):
        url = f"https://slack.com/api/{method}"
        headers = {"Authorization": f"Bearer {self.token}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            r = requests.post(url, headers=headers, json=json_body, timeout=20)
        else:
            r = requests.get(url, headers=headers, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack {method} error: {data.get('error')}")
        return data

    def history(self, oldest):
        return self._call("conversations.history", params={
            "channel": self.channel,
            "oldest": oldest,
            "limit": 200,
            "inclusive": "false",
        })

    def post_reply(self, thread_ts, text):
        return self._call("chat.postMessage", json_body={
            "channel": self.channel,
            "thread_ts": thread_ts,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        })

    def whoami(self):
        return self._call("auth.test")


# ── YouTube ────────────────────────────────────────────────────────────────────

def yt_title(video_id):
    """No-auth title fetch via oEmbed. Returns 'video' on failure."""
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://youtu.be/{video_id}", "format": "json"},
            timeout=10,
        )
        if r.ok:
            return r.json().get("title", "video")
    except Exception:
        pass
    return "video"


def yt_transcript_via_clipmaker(url):
    """Fetch YouTube transcript by POSTing to ClipMaker's /api/transcript on the
    user's Mac (exposed via a public tunnel). Bot lives in GH Actions cloud where
    YouTube blocks yt-dlp; ClipMaker on the Mac uses a residential IP and works
    reliably. Returns ({segments}, title, error).
    """
    base = os.environ.get("CLIPMAKER_URL", "").rstrip("/")
    if not base:
        return None, None, "CLIPMAKER_URL is not set"
    token = os.environ.get("CLIPMAKER_AUTH_TOKEN") or ""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        r = requests.post(f"{base}/api/transcript",
                          headers=headers, json={"url": url}, timeout=200)
    except requests.exceptions.RequestException as e:
        return None, None, f"ClipMaker unreachable — is your Mac on and the tunnel running? ({type(e).__name__})"

    if r.status_code == 401:
        return None, None, "ClipMaker rejected the auth token"
    if r.status_code >= 500:
        try:
            msg = r.json().get("error", r.text[:200])
        except Exception:
            msg = r.text[:200]
        return None, None, f"ClipMaker error: {msg}"
    if not r.ok:
        return None, None, f"HTTP {r.status_code}: {r.text[:200]}"

    data = r.json()
    return data.get("segments") or [], data.get("title") or "video", None


# ── Twitter/X ──────────────────────────────────────────────────────────────────

def fetch_tweet(url):
    """Fetch a tweet's text + author via Twitter's public oembed API.

    Returns {"text", "author_name", "author_handle", "has_video"} or None on
    failure (deleted / protected / suspended).
    """
    try:
        r = requests.get(
            "https://publish.twitter.com/oembed",
            params={"url": url, "omit_script": "true", "hide_thread": "true",
                    "dnt": "true"},
            timeout=15,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  ! tweet fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None

    html_body = data.get("html", "")
    # oembed HTML: <blockquote><p>tweet text with <a>links</a></p>&mdash; Author ...</blockquote>
    from html import unescape
    p_match = re.search(r"<p[^>]*>(.*?)</p>", html_body, re.DOTALL)
    if not p_match:
        return None
    text = re.sub(r"<[^>]+>", " ", p_match.group(1))
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    # Detect embedded video via the pic.twitter.com marker in the raw HTML.
    # Cheap heuristic — oembed doesn't tell us the media type directly.
    has_video = "video.twimg.com" in html_body or "video/" in html_body.lower()

    author_name = data.get("author_name", "").strip()
    author_url = data.get("author_url", "") or ""
    handle_match = re.search(r"(?:twitter\.com|x\.com)/([^/]+)/?$", author_url)
    author_handle = handle_match.group(1) if handle_match else ""

    return {
        "text": text,
        "author_name": author_name,
        "author_handle": author_handle,
        "has_video": has_video,
    }


TWEET_PROMPT = """You are analyzing a single tweet for a reporter at Jewish Insider, a \
publication covering Jewish, Israel, and Middle East policy, politics, and community affairs.

Tweet author: {author}
Tweet text: {text}

Judge whether this tweet is news-making from a JI editorial angle. News-making means:
  - Specific policy claim, commitment, or reversal
  - Controversial or notable statement about Israel, Iran, Gaza, Hamas, Hezbollah, \
antisemitism, U.S.-Israel relations, Congress, the administration, or upcoming races
  - Scoop, revelation, or first-time-said remark
  - Quotable pull-quote from a policymaker, expert, or public figure JI covers

NOT news-making:
  - Retweets with no added claim, generic partisan snark, unrelated topics
  - Pleasantries, self-promotion, personal life posts, off-beat topics

Return ONE JSON object:
{{
  "news_making": true/false,
  "headline": "news-headline-style one-liner, action/claim-focused, 15 words max. \
Empty string if not news-making.",
  "why": "one short sentence on what makes this news for JI, or why it isn't."
}}

Return ONLY the JSON object. No markdown fences.
"""


def summarize_tweet(tweet):
    from google import genai
    from google.genai import types as gtypes

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    client = genai.Client(api_key=key)

    author = tweet["author_name"]
    if tweet["author_handle"]:
        author = f"{author} (@{tweet['author_handle']})"

    prompt = TWEET_PROMPT.format(author=author, text=tweet["text"])
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=gtypes.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=512,
        ),
    )
    raw = (response.text or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        return json.loads(raw)


def build_tweet_reply(tweet, verdict):
    author = tweet["author_name"] or "unknown author"
    handle = f" (@{tweet['author_handle']})" if tweet["author_handle"] else ""
    header = f"*Tweet from {_esc(author)}{_esc(handle)}*"
    tweet_quoted = "\n".join(f"> {_esc(line)}" for line in tweet["text"].split("\n"))

    if not verdict.get("news_making"):
        why = str(verdict.get("why", "")).strip()
        body = f"_Not obviously news-making — {_esc(why)}_" if why else "_Not obviously news-making._"
        return f"{header}\n\n{tweet_quoted}\n\n{body}"

    headline = _esc(str(verdict.get("headline", "")).strip())
    return f"{header}\n\n📌 {headline}\n\n{tweet_quoted}"


# ── YouTube helpers (continued) ────────────────────────────────────────────────

def format_transcript_for_llm(segments):
    """Convert transcript segments into timestamped lines for the LLM."""
    lines = []
    for seg in segments:
        start = int(seg["start"])
        m, s = start // 60, start % 60
        text = seg["text"].replace("\n", " ").strip()
        if text:
            lines.append(f"[{m:02d}:{s:02d}] {text}")
    return "\n".join(lines)


# ── Gemini summarizer ──────────────────────────────────────────────────────────

def summarize(transcript_text, video_title):
    from google import genai
    from google.genai import types as gtypes

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    client = genai.Client(api_key=key)

    prompt = SUMMARY_PROMPT.format(title=video_title, transcript=transcript_text)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=gtypes.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=4096,
        ),
    )
    raw = (response.text or "").strip()
    try:
        moments = json.loads(raw)
    except json.JSONDecodeError:
        # Strip any markdown fences and retry
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        moments = json.loads(raw)
    if not isinstance(moments, list):
        return []
    return moments


# ── Reply formatting ───────────────────────────────────────────────────────────

def build_reply(title, moments):
    if not moments:
        return f"Scanned *{_esc(title)}* — no clearly news-making moments jumped out."
    lines = [f"*Notable Moments from “{_esc(title)}”*", ""]
    for i, m in enumerate(moments, 1):
        s = int(m.get("start_min", 0))
        e = int(m.get("end_min", s))
        rng = f"{s}min" if s == e else f"{s}-{e}min"
        headline = _esc(str(m.get("headline", "")).strip())
        lines.append(f"{i}. {headline} ({rng})")
        quote = str(m.get("quote", "")).strip()
        if quote:
            lines.append(f"    > “{_esc(quote)}”")
    return "\n".join(lines)


def _esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── State ──────────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_ts": None, "processed_ts": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


# ── URL processing ─────────────────────────────────────────────────────────────

def process_url(url, dry_run=False, slack=None, thread_ts=None):
    """Fetch transcript/tweet, summarize, format reply. Optionally post to Slack."""
    yt_match = YOUTUBE_RE.search(url)
    tweet_match = TWEET_RE.search(url)
    pod_match = PODCAST_RE.search(url)

    if yt_match:
        video_id = yt_match.group(1)
        print(f"  → YouTube video {video_id}")
        segments, cm_title, err = yt_transcript_via_clipmaker(url)
        # Prefer the title ClipMaker gave us; fall back to oembed if it's the
        # generic 'video' placeholder.
        title = cm_title if cm_title and cm_title != "video" else yt_title(video_id)
        if err:
            reply = f"Couldn't summarize *{_esc(title)}* — {_esc(err)}"
        elif not segments:
            reply = f"Couldn't fetch a transcript for *{_esc(title)}* — the video may not have English captions."
        else:
            transcript_text = format_transcript_for_llm(segments)
            print(f"  → {len(segments)} segments, ~{len(transcript_text)} chars")
            moments = summarize(transcript_text, title)
            print(f"  → {len(moments)} notable moments")
            reply = build_reply(title, moments)
    elif tweet_match:
        tweet_id = tweet_match.group(1)
        print(f"  → Tweet {tweet_id}")
        tweet = fetch_tweet(url)
        if not tweet:
            reply = "Couldn't fetch that tweet — it may be deleted, protected, or from a suspended account."
        elif tweet["has_video"] and len(tweet["text"]) < 40:
            # Likely a video tweet with minimal caption — Whisper needed.
            reply = "This tweet has an embedded video — video transcription is coming in v2."
        else:
            verdict = summarize_tweet(tweet)
            print(f"  → news_making={verdict.get('news_making')}")
            reply = build_tweet_reply(tweet, verdict)
    elif pod_match:
        reply = (
            "Podcast transcript summaries are coming in v2 — for now this bot "
            "only handles YouTube and X/Twitter. (Spotify/Apple Podcasts don't "
            "expose free transcripts, so we'd need to add Whisper.)"
        )
    else:
        return None  # no supported URL

    if dry_run or not slack:
        print("--- REPLY ---")
        print(reply)
        print("-------------")
    else:
        slack.post_reply(thread_ts, reply)
        print(f"  → posted reply in thread {thread_ts}")
    return reply


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Print replies instead of posting to Slack.")
    parser.add_argument("--url", help="Process one URL and exit (implies --dry-run).")
    parser.add_argument("--window-hours", type=int, default=None,
                        help="Override baseline window on first run (default: 1 hour).")
    args = parser.parse_args()

    if args.url:
        process_url(args.url, dry_run=True)
        return

    slack = Slack()
    if not slack.token or not slack.channel:
        sys.exit("Missing SLACK_BOT_TOKEN or SLACK_CHANNEL_ID.")
    bot_user_id = slack.whoami().get("user_id")

    state = load_state()
    now = time.time()
    baseline_window = (args.window_hours or 1) * 3600
    since = state.get("last_ts") or f"{now - baseline_window:.6f}"
    first_run = not state.get("last_ts")

    print(f"Polling since ts={since} ({'first run baseline' if first_run else 'resume'})")
    history = slack.history(since)

    messages = history.get("messages", [])
    print(f"  → {len(messages)} messages in window")

    processed = set(state.get("processed_ts", []))
    max_ts = float(since)

    # Slack returns newest-first — process oldest-first so replies land in order.
    for msg in reversed(messages):
        ts = msg["ts"]
        max_ts = max(max_ts, float(ts))
        if ts in processed:
            continue
        # Skip messages from the bot itself + other bots.
        if msg.get("user") == bot_user_id or msg.get("bot_id"):
            continue
        text = msg.get("text", "") or ""
        if not (YOUTUBE_RE.search(text) or TWEET_RE.search(text) or PODCAST_RE.search(text)):
            continue

        # Baseline the very first run: mark as processed but don't post.
        if first_run and not args.dry_run:
            print(f"  (baseline) skip {ts}")
            processed.add(ts)
            continue

        # First supported URL per message — one summary per post.
        url_match = YOUTUBE_RE.search(text) or TWEET_RE.search(text) or PODCAST_RE.search(text)
        url = url_match.group(0)
        print(f"[{ts}] processing: {url}")
        try:
            process_url(url, dry_run=args.dry_run, slack=slack, thread_ts=ts)
            processed.add(ts)
        except Exception as e:
            print(f"  ! failed: {type(e).__name__}: {e}", file=sys.stderr)
            # Don't add to processed — retry next tick.

    if not args.dry_run:
        state["last_ts"] = f"{max_ts:.6f}"
        state["processed_ts"] = sorted(processed)[-500:]  # cap size
        save_state(state)


if __name__ == "__main__":
    main()
