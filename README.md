# JI Podcast Summarizer

Watches a Slack channel for YouTube URLs and posts a threaded reply summarizing the notable moments — tuned for Jewish Insider's news-making priorities.

Same GitHub Actions polling pattern as the other JI watchers (`ji-article-watcher`, `fec-watcher`, `ji-govt-watcher`).

## What it does

When someone posts a YouTube link in **#podcasts-plus-youtube**, the bot replies in-thread within ~2 minutes with:

```
Notable Moments from "Sen. Warren on Iran War Powers"

1. Says Iran war would be "illegal without Congressional authorization" (20-30min)
    > "The Constitution is unambiguous. Only Congress can declare war."

2. Argues Congress should reject any Trump Iran deal (25-35min)
    > "This administration cannot be trusted..."

3. Era of US aid to Israel is "coming to an end" (38-45min)
```

Spotify / Apple Podcasts links get a "not supported yet" reply until v2.

## Local smoke test

```bash
cd ~/ji-podcast-summarizer
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY=...       # from https://aistudio.google.com/apikey
python3 poll.py --url "https://www.youtube.com/watch?v=<video>"
```

This fetches the transcript, calls Gemini, and prints the reply without posting to Slack.

## One-time setup

### 1. Slack App

1. Go to https://api.slack.com/apps → **Create New App** → From scratch → name it `ji-podcast-summarizer` in the JI workspace.
2. **OAuth & Permissions** → add Bot Token Scopes:
   - `channels:history` (or `groups:history` if #podcasts-plus-youtube is private)
   - `chat:write`
3. Install to workspace. Copy the **Bot User OAuth Token** (starts with `xoxb-`).
4. In Slack, `/invite @ji-podcast-summarizer` in #podcasts-plus-youtube.
5. Get the channel ID: right-click the channel → **View channel details** → scroll to bottom → copy the `C0...` ID.

### 2. Gemini API key

Free at https://aistudio.google.com/apikey. Volume here is well under the free cap.

### 3. GitHub repo + secrets

```bash
cd ~/ji-podcast-summarizer
git init
git add .
git commit -m "Initial commit"
gh repo create sruli-hue/ji-podcast-summarizer --private --source=. --push
```

Then in **Settings → Secrets and variables → Actions** on the repo, add:

| Secret | Value |
| --- | --- |
| `SLACK_BOT_TOKEN` | The `xoxb-…` token from step 1 |
| `SLACK_CHANNEL_ID` | The `C0…` ID from step 1 |
| `GEMINI_API_KEY` | The key from step 2 |

### 4. cron-job.org pinger (for ~2-min cadence)

The GH Actions `schedule:` block runs every 10 min but Actions cron is unreliable. For faster cadence, set up a pinger (same as your other watchers):

1. Create a fine-grained GitHub PAT with `actions: write` scope on this repo.
2. On cron-job.org, POST every 2 min to:
   ```
   https://api.github.com/repos/sruli-hue/ji-podcast-summarizer/actions/workflows/poll.yml/dispatches
   ```
   with header `Authorization: Bearer <PAT>` and body `{"ref":"main"}`.

## How the state file works

`watcher_state.json` stores the last processed message timestamp and the last 500 processed message IDs. The workflow commits it back to the repo every run so no message is ever processed twice, and nothing gets missed between runs.

**First run:** the bot silently baselines everything already in the channel — no dump of old links.

## Costs

- **Slack API:** free
- **YouTube transcript API:** free
- **Gemini 2.5 Flash:** well within free tier at this volume (a few summaries per day)
- **GitHub Actions:** free tier is 2,000 minutes/month — this uses ~15 sec per tick, ~5-10 min/day
