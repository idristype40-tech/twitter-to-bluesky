# Twitter → Bluesky Mirror Bot

Automatically mirrors tweets to Bluesky, including images, clickable hashtags, and hyperlinks.



**Example Accounts**
- Twitter Account: 
- Bluesky Mirror: 

---

## Project Overview

This lightweight Python bot will:

1. **Fetch** the latest tweets (excluding retweets/replies) from the Infinity Nikki Twitter account via a [xitter](https://xitter.cf/) RSS feed — no Twitter API key required
2. **Parse** images from the RSS feed and upload them to Bluesky
3. **Annotate** hashtags and URLs as proper clickable facets on Bluesky
4. **Repost** each new tweet as a Bluesky post using the AT Protocol client
5. **Track** which tweets have already been reposted in a simple JSON file
6. **Automate** execution on a cron schedule with GitHub Actions (free tier)

---

## Tech Stack & Tools

- **Language & Runtime**
  - Python 3.12+
- **Core Libraries**
  - [`atproto`](https://pypi.org/project/atproto/) — Bluesky AT Protocol client
  - [`feedparser`](https://pypi.org/project/feedparser/) — RSS feed parser
  - [`requests`](https://pypi.org/project/requests/) — HTTP client for image downloads
  - [`beautifulsoup4`](https://pypi.org/project/beautifulsoup4/) — HTML parser for extracting images from RSS
- **External Services**
  - **xitter RSS** — scrape-free Twitter feed via `https://xitter.net/<handle>/rss`
  - **Bluesky AT Protocol** — post content to Bluesky
- **State Persistence**
  - `seen_ids.json` — local JSON file storing reposted tweet IDs
- **CI/CD & Scheduling**
  - **GitHub Actions** — runs `main.py` every 30 minutes (or on manual dispatch)
  - **Git & GitHub** — source control, secret storage, workflow orchestration

---

## Getting Started

### 1. Fork or clone the repo

```bash
git clone https://github.com/<your-username>/twitter-to-bluesky.git
cd twitter-to-bluesky
```

### 2. Create a Python virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Initialize your state file

```bash
echo '[]' > seen_ids.json
```

---

## Configuration

The bot reads credentials from environment variables. Only two are needed.

| Variable           | Description                                      |
|--------------------|--------------------------------------------------|
| `BLUESKY_HANDLE`   | Your Bluesky handle (e.g. `you.bsky.social`)     |
| `BLUESKY_PASSWORD` | A Bluesky **App Password** (not your main password) |

To generate a Bluesky App Password: **Settings → Privacy and Security → App Passwords**

#### Locally

```bash
export BLUESKY_HANDLE="you.bsky.social"
export BLUESKY_PASSWORD="xxxx-xxxx-xxxx-xxxx"
python main.py
```

#### In GitHub Actions

1. Go to **Settings → Secrets and variables → Actions** in your repo
2. Add `BLUESKY_HANDLE` and `BLUESKY_PASSWORD` as repository secrets

---

## Deployment via GitHub Actions

The workflow file at `.github/workflows/repost.yml`:

- Checks out your code
- Sets up Python 3.12
- Installs dependencies
- Runs `main.py`
- Commits the updated `seen_ids.json` back to the repo

### Key points

- **Schedule**: runs every 30 minutes (`cron: '*/30 * * * *'`)
- **Manual dispatch**: click **Run workflow** in the Actions tab at any time
- **Permissions**: make sure "Read and write repository contents" is enabled under **Settings → Actions → General**

No Twitter API key, no external servers, no credit card — everything runs on GitHub's free tier.

---

## How It Works

1. **Fetch RSS** from `https://xitter.net/infinitynikkien/rss`
2. **Filter** out retweets and replies
3. **Extract images** from the RSS `<description>` HTML and convert xitter image URLs to real Twitter CDN URLs
4. **Parse facets** — detect URLs and hashtags in the tweet text and annotate them for Bluesky's rich text system (required for clickable links/hashtags)
5. **Upload images** to Bluesky and attach them as an embed
6. **Post** to Bluesky with text, facets, and images
7. **Persist state** in `seen_ids.json` and push back to GitHub

## Why xitter instead of the Twitter API?

The Twitter/X API now costs $200/month for basic read access. Nittxitterer is an open-source Twitter front-end that exposes a free RSS feed, requiring no API key or authentication. This project uses that RSS feed instead.

Please be considerate of xitter instance maintainers — the 30 minute poll interval is intentional to avoid hammering their servers.
