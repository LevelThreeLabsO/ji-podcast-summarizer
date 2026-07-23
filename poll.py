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
# Generic http(s) URL — used as a fallback for news articles etc. Comes LAST
# in dispatch so YT/Twitter/podcast URLs take priority.
ARTICLE_RE = re.compile(r"https?://[^\s>|<]+")

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
  "headline": "news-headline-style one-liner packed with the specifics a JI editor would want at-a-glance",
  "start_min": <int minute>,
  "end_min": <int minute>,
  "quote": "a direct verbatim pull-quote from the transcript, under 200 chars, or empty string if no clean one exists"
}}

Rules for the headline:
  - Name specifics when it's reliable to do so — which people, which groups/orgs, which \
bill numbers, which countries, which dollar amounts, which dates. If a specific is clearly \
stated in the transcript, include it. If it isn't, don't invent one and don't guess; leave \
it out or hedge honestly (e.g. "an unnamed group", "several senators").
  - Active voice, claim-focused. Aim for 20-30 words if the specifics need it. \
Do not sacrifice a reliable specific to hit a word count.
  - No filler ("discusses", "talks about", "mentions") — go straight to what was said/claimed.
  - Bracketed clarifier OK if extra context is needed, e.g. "(related to Platner)".

Rules for the quote:
  - Must appear VERBATIM in the transcript — do not paraphrase, condense, or clean up.
  - Under 200 chars. If no clean stand-alone pull-quote exists, empty string.

Other:
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

    def thread_has_bot_reply(self, thread_ts, bot_user_id=None):
        """Return True if this bot (or any bot) has already replied in-thread.
        Belt-and-suspenders check to prevent duplicate summaries from state-race
        conditions between workflow runs."""
        try:
            r = self._call("conversations.replies", params={
                "channel": self.channel,
                "ts": thread_ts,
                "limit": 20,
            })
        except Exception:
            return False  # on error, don't block posting
        # Skip the parent message (index 0); any reply from us or another bot counts.
        for m in r.get("messages", [])[1:]:
            if bot_user_id and m.get("user") == bot_user_id:
                return True
            if m.get("bot_id"):
                return True
        return False

    def post_reply(self, thread_ts, text, broadcast=False):
        # broadcast=True: also surface the message in the main channel feed
        # (used for real summaries). broadcast=False: quiet thread-only reply
        # (used for "couldn't summarize" / "not supported" notices so they
        # don't clutter the channel).
        return self._call("chat.postMessage", json_body={
            "channel": self.channel,
            "thread_ts": thread_ts,
            "reply_broadcast": broadcast,
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


def podcast_transcript_via_clipmaker(url):
    """Fetch podcast audio transcript by POSTing to ClipMaker's
    /api/podcast-transcript on the user's Mac. ClipMaker downloads the audio
    via yt-dlp and transcribes it with Groq's free Whisper endpoint. Returns
    ({segments}, title, error)."""
    base = os.environ.get("CLIPMAKER_URL", "").rstrip("/")
    if not base:
        return None, None, "CLIPMAKER_URL is not set"
    token = os.environ.get("CLIPMAKER_AUTH_TOKEN") or ""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        # Longer timeout — Whisper on a 60-min podcast can take ~30-60 sec.
        r = requests.post(f"{base}/api/podcast-transcript",
                          headers=headers, json={"url": url}, timeout=900)
    except requests.exceptions.RequestException as e:
        return None, None, f"ClipMaker unreachable — is your Mac on and the tunnel running? ({type(e).__name__})"

    if r.status_code == 401:
        return None, None, "ClipMaker rejected the auth token"
    if r.status_code == 429:
        return None, None, "Groq rate limited (free-tier daily cap likely hit)"
    if r.status_code >= 500:
        try:
            msg = r.json().get("error", r.text[:200])
        except Exception:
            msg = r.text[:200]
        return None, None, f"ClipMaker error: {msg}"
    if not r.ok:
        return None, None, f"HTTP {r.status_code}: {r.text[:200]}"

    data = r.json()
    return data.get("segments") or [], data.get("title") or "podcast", None


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
    author = tweet["author_name"]
    if tweet["author_handle"]:
        author = f"{author} (@{tweet['author_handle']})"
    prompt = TWEET_PROMPT.format(author=author, text=tweet["text"])
    response = _gemini_generate_with_retry(prompt, max_tokens=512)
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

def _gemini_generate_with_retry(prompt, max_tokens=4096):
    """Call Gemini with retries + model fallback. Gemini's 'flash-latest' can
    hit 503 UNAVAILABLE (high demand). We retry with backoff, and if the fast
    model keeps failing, fall back to a lite variant."""
    from google import genai
    from google.genai import types as gtypes
    from google.genai import errors as gerrors

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    client = genai.Client(api_key=key)

    # Order: main model, then a lite fallback that's usually less contended.
    models = ["gemini-flash-latest", "gemini-flash-lite-latest"]
    config = gtypes.GenerateContentConfig(
        response_mime_type="application/json",
        max_output_tokens=max_tokens,
    )

    last_err = None
    for model in models:
        for attempt in range(3):
            try:
                return client.models.generate_content(
                    model=model, contents=prompt, config=config,
                )
            except (gerrors.ServerError, gerrors.APIError) as e:
                last_err = e
                # Sleep 4s, 12s before the next attempt within the same model.
                if attempt < 2:
                    time.sleep(4 * (attempt * 2 + 1))
                    continue
                # Otherwise fall through to the next model.
                break
    # If we're here, everything failed.
    raise last_err if last_err else RuntimeError("Gemini call failed for unknown reason")


def summarize(transcript_text, video_title):
    prompt = SUMMARY_PROMPT.format(title=video_title, transcript=transcript_text)
    response = _gemini_generate_with_retry(prompt, max_tokens=4096)
    raw = (response.text or "").strip()
    try:
        moments = json.loads(raw)
    except json.JSONDecodeError:
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        moments = json.loads(raw)
    if not isinstance(moments, list):
        return []
    return moments


# ── Article fetch + summary ────────────────────────────────────────────────────

ARTICLE_PROMPT = """You are analyzing a news article for a reporter at Jewish Insider, \
a publication covering Jewish, Israel, and Middle East policy, politics, and community affairs. \
Surface the NOTABLE POINTS a JI editor would flag in Slack.

What counts as notable — same JI-beat rubric as our video summaries:
  - Specific policy claims, commitments, or reversals
  - News-making statements on Israel, Iran, Gaza, Hamas, Hezbollah, antisemitism, \
U.S.-Israel relations, Congress, the administration, upcoming races
  - Quotable pull-quotes that stand on their own
  - Scoops, revelations, first-time-said remarks

Article title: {title}

ARTICLE TEXT:
{text}

Return a JSON array of 3-6 items. Each item:
{{
  "headline": "news-headline-style one-liner packed with specifics a JI editor would want at-a-glance",
  "quote": "a direct verbatim pull-quote from the article, under 200 chars, or empty string if no clean one exists"
}}

Rules for the headline:
  - Name specifics when reliable — which people, which groups/orgs, which bill numbers, which \
countries, which dollar amounts, which dates. If a specific is clearly stated in the article, \
include it. If it isn't, don't invent one; leave it out or hedge honestly.
  - Active voice, claim-focused. 20-30 words if specifics need it. No filler.

Rules for the quote:
  - Must appear VERBATIM in the article text — do not paraphrase or clean up.
  - Under 200 chars. Empty string if no clean pull-quote exists.

If there are truly no notable moments, return [].
Return ONLY the JSON array. No markdown fences.
"""


# Curated User-Agent — real Chrome string, less likely to be blocked than a bare requests default.
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")


def fetch_article(url):
    """Fetch and extract article text. Returns {"title", "text"} or None.

    Works well for most news sites (CNN, AP, Politico, Axios, etc.). Fails on
    aggressive paywalls (NYTimes, WaPo, WSJ) that require auth cookies.
    """
    try:
        import trafilatura
    except ImportError:
        print("  ! trafilatura not installed", file=sys.stderr)
        return None

    try:
        r = requests.get(
            url,
            headers={"User-Agent": _BROWSER_UA,
                     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                     "Accept-Language": "en-US,en;q=0.9"},
            timeout=25,
            allow_redirects=True,
        )
    except requests.exceptions.RequestException as e:
        print(f"  ! article fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None

    if r.status_code >= 400:
        print(f"  ! article fetch HTTP {r.status_code}", file=sys.stderr)
        return None

    try:
        extracted = trafilatura.extract(
            r.text, output_format="json", with_metadata=True,
        )
    except Exception as e:
        print(f"  ! trafilatura extract failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    if not extracted:
        return None
    try:
        data = json.loads(extracted)
    except json.JSONDecodeError:
        return None
    text = (data.get("text") or "").strip()
    if len(text) < 300:
        # Anything shorter than ~300 chars is almost certainly a paywall stub
        # or a "please enable JS" placeholder — not a real article.
        return None
    return {"title": (data.get("title") or "").strip(), "text": text}


def summarize_article(text, article_title):
    prompt = ARTICLE_PROMPT.format(title=article_title, text=text[:60000])
    response = _gemini_generate_with_retry(prompt, max_tokens=4096)
    raw = (response.text or "").strip()
    try:
        moments = json.loads(raw)
    except json.JSONDecodeError:
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        moments = json.loads(raw)
    if not isinstance(moments, list):
        return []
    return moments


def build_article_reply(title, url, moments):
    if not moments:
        return f"Scanned *{_esc(title)}* — no clearly news-making points jumped out."
    from urllib.parse import urlparse
    host = urlparse(url).netloc.replace("www.", "")
    lines = [f"*Notable Points from “{_esc(title)}”* _({_esc(host)})_", ""]
    for i, m in enumerate(moments, 1):
        headline = _esc(str(m.get("headline", "")).strip())
        lines.append(f"{i}. *{headline}*")
        quote = str(m.get("quote", "")).strip()
        if quote:
            lines.append(f"    • “{_esc(quote)}”")
    return "\n".join(lines)


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
        lines.append(f"{i}. *{headline}* ({rng})")
        quote = str(m.get("quote", "")).strip()
        if quote:
            lines.append(f"    • “{_esc(quote)}”")
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

class TransientError(Exception):
    """Raised when a URL couldn't be processed for a reason that will likely
    resolve itself (Mac off, tunnel down, Gemini overloaded, network hiccup).

    Bubbles to the main loop, which does NOT advance the polling window past
    the message — so it gets retried on the next tick.
    """


# Substrings that indicate a ClipMaker-reachability failure vs a video-specific
# issue. If any of these appear in the error string, treat as transient.
_TRANSIENT_HINTS = (
    "ClipMaker unreachable",
    "ClipMaker rejected the auth token",
    "CLIPMAKER_URL is not set",
    "HTTP 5",             # any 5xx from the tunnel/ClipMaker
    "ClipMaker error:",   # generic ClipMaker error passthrough
)


def _is_transient(err_str):
    if not err_str:
        return False
    return any(h in err_str for h in _TRANSIENT_HINTS)


def process_url(url, dry_run=False, slack=None, thread_ts=None):
    """Fetch transcript/tweet/article, summarize, post to Slack.

    Returns (reply_or_none, is_summary):
      - (reply, True)  → real summary, post + broadcast, mark processed
      - (reply, False) → non-summary content (currently unused — nothing posts
                         on failure per the "silent retry" behavior)
      - (None, False)  → nothing to do (unsupported URL, or nothing news-making)

    Raises TransientError if the bot literally couldn't run (Mac off, Gemini
    503, etc.) — main loop retries next tick without posting anything.
    """
    yt_match = YOUTUBE_RE.search(url)
    tweet_match = TWEET_RE.search(url)
    pod_match = PODCAST_RE.search(url)
    article_match = ARTICLE_RE.search(url) if not (yt_match or tweet_match or pod_match) else None

    if yt_match:
        video_id = yt_match.group(1)
        print(f"  → YouTube video {video_id}")
        segments, cm_title, err = yt_transcript_via_clipmaker(url)
        if err and _is_transient(err):
            raise TransientError(f"YT transcript unreachable: {err}")
        if err or not segments:
            # Permanent-ish: no captions, video removed, etc. Skip silently.
            print(f"  → no transcript (permanent): {err or 'no segments'}")
            return None, False
        title = cm_title if cm_title and cm_title != "video" else yt_title(video_id)
        transcript_text = format_transcript_for_llm(segments)
        print(f"  → {len(segments)} segments, ~{len(transcript_text)} chars")
        moments = summarize(transcript_text, title)
        print(f"  → {len(moments)} notable moments")
        if not moments:
            return None, False
        reply = build_reply(title, moments)
        is_summary = True
    elif tweet_match:
        tweet_id = tweet_match.group(1)
        print(f"  → Tweet {tweet_id}")
        tweet = fetch_tweet(url)
        if not tweet:
            print("  → tweet fetch failed — deleted/protected/suspended (permanent)")
            return None, False
        if tweet["has_video"] and len(tweet["text"]) < 40:
            print("  → video-only tweet (permanent skip until v2)")
            return None, False
        verdict = summarize_tweet(tweet)
        print(f"  → news_making={verdict.get('news_making')}")
        if not verdict.get("news_making"):
            return None, False
        reply = build_tweet_reply(tweet, verdict)
        is_summary = True
    elif article_match:
        print(f"  → Article {url}")
        article = fetch_article(url)
        if not article:
            # Paywall, JS-required site, extraction failure. Permanent skip.
            print("  → article fetch/extract failed (paywall / permanent)")
            return None, False
        title = article["title"] or url
        print(f"  → {len(article['text'])} chars of article text")
        moments = summarize_article(article["text"], title)
        print(f"  → {len(moments)} notable moments")
        if not moments:
            return None, False
        reply = build_article_reply(title, url, moments)
        is_summary = True
    elif pod_match:
        print(f"  → Podcast {url}")
        segments, pod_title, err = podcast_transcript_via_clipmaker(url)
        if err and _is_transient(err):
            raise TransientError(f"Podcast transcript unreachable: {err}")
        if err or not segments:
            print(f"  → podcast transcript unavailable (permanent): {err or 'no segments'}")
            return None, False
        title = pod_title or "podcast"
        transcript_text = format_transcript_for_llm(segments)
        print(f"  → {len(segments)} segments, ~{len(transcript_text)} chars")
        moments = summarize(transcript_text, title)
        print(f"  → {len(moments)} notable moments")
        if not moments:
            return None, False
        reply = build_reply(title, moments)
        is_summary = True
    else:
        return None, False

    if dry_run or not slack:
        print("--- REPLY ---")
        print(reply)
        print("-------------")
    else:
        slack.post_reply(thread_ts, reply, broadcast=is_summary)
        print(f"  → posted reply in thread {thread_ts} (broadcast={is_summary})")
    return reply, is_summary


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
    # Track the newest ts we can safely advance the polling window past.
    # We only advance past a message if it's been fully handled (processed,
    # baseline-skipped, or contains no URL we care about). If a message
    # failed, `advanceable_ts` freezes at the previous value so next tick
    # will re-fetch that message and retry.
    advanceable_ts = float(since)
    any_failed = False

    # Slack returns newest-first — process oldest-first so replies land in order.
    for msg in reversed(messages):
        ts = msg["ts"]
        ts_f = float(ts)
        if ts in processed:
            if not any_failed:
                advanceable_ts = max(advanceable_ts, ts_f)
            continue
        # Skip messages from the bot itself + other bots.
        if msg.get("user") == bot_user_id or msg.get("bot_id"):
            if not any_failed:
                advanceable_ts = max(advanceable_ts, ts_f)
            continue
        text = msg.get("text", "") or ""
        if not (YOUTUBE_RE.search(text) or TWEET_RE.search(text) or PODCAST_RE.search(text) or ARTICLE_RE.search(text)):
            if not any_failed:
                advanceable_ts = max(advanceable_ts, ts_f)
            continue

        # Baseline the very first run: mark as processed but don't post.
        if first_run and not args.dry_run:
            print(f"  (baseline) skip {ts}")
            processed.add(ts)
            if not any_failed:
                advanceable_ts = max(advanceable_ts, ts_f)
            continue

        # Belt-and-suspenders duplicate-prevention: if the bot already replied
        # in this thread (from a prior workflow run whose state-commit lost a
        # race, etc.), skip. Marks as processed so we don't keep re-checking.
        if not args.dry_run and slack.thread_has_bot_reply(ts, bot_user_id):
            print(f"  (already replied in-thread) skip {ts}")
            processed.add(ts)
            if not any_failed:
                advanceable_ts = max(advanceable_ts, ts_f)
            continue

        # First supported URL per message — one summary per post.
        url_match = YOUTUBE_RE.search(text) or TWEET_RE.search(text) or PODCAST_RE.search(text) or ARTICLE_RE.search(text)
        url = url_match.group(0)
        print(f"[{ts}] processing: {url}")
        try:
            process_url(url, dry_run=args.dry_run, slack=slack, thread_ts=ts)
            # Mark processed on both success AND permanent failure — bot did
            # what it could, no point retrying (paywall won't unpaywall itself).
            processed.add(ts)
            if not any_failed:
                advanceable_ts = max(advanceable_ts, ts_f)
        except TransientError as e:
            print(f"  ! transient — will retry next tick: {e}", file=sys.stderr)
            # Silent: no reply posted, ts not marked. Next tick fetches this
            # message again and tries again. When your Mac / tunnel / Gemini
            # comes back, the summary posts as if nothing happened.
            any_failed = True
        except Exception as e:
            print(f"  ! unexpected failure: {type(e).__name__}: {e}", file=sys.stderr)
            # Unknown error — treat as transient to be safe (retry next tick).
            any_failed = True

    if not args.dry_run:
        state["last_ts"] = f"{advanceable_ts:.6f}"
        state["processed_ts"] = sorted(processed)[-500:]  # cap size
        save_state(state)


if __name__ == "__main__":
    main()
