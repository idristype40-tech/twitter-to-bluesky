import calendar
import functools
import io
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime

import feedparser
import requests
from atproto import Client, models
from bs4 import BeautifulSoup
from PIL import Image

# ---------------------------------------------------------------- settings --
TWITTER_USER = "InfinityNikkiEN"
FEED_HOSTS = [                # mirrors tried in order until one returns tweets
    "https://xitter.cf",      # add backup mirrors here, e.g. "https://some-other-mirror.example"
]
feed_host = FEED_HOSTS[0]     # the mirror that actually worked this run (set by read_feed)

BLUESKY_HANDLE = os.environ["BLUESKY_HANDLE"]
BLUESKY_PASSWORD = os.environ["BLUESKY_PASSWORD"]

STATE_FILE = "seen_ids.json"
MAX_THREADS_KEPT = 50        # how many past threads to remember (for replies)
FIRST_RUN_LIMIT = 10         # on the very first run, only post this many newest tweets
MAX_ATTEMPTS = 3             # give up on a tweet that fails this many runs in a row
PINNED = "PINNED"            # marker: the reply sits under the pinned tweet

VIDEO_WAIT_SECONDS = 180     # how long to wait for Bluesky to process a video
OCR_ALT_TEXT = True          # read text inside images (RapidOCR) and use it as alt text
OCR_MIN_SCORE = 0.6          # ignore OCR lines it is less sure about (0 to 1)
MAX_VIDEO_SECONDS = 600
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_VIDEO_HEIGHT = 1080


# ------------------------------------------------------------------- state --
def load_state():
    """Return (set of seen tweet ids, dict of threads we've posted, fail counts)."""
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if isinstance(data, list):          # old format: just a list of ids
        data = {"seen": data}
    return set(data.get("seen", [])), data.get("threads", {}), data.get("fails", {})


def save_state(seen, threads, fails):
    newest = sorted(threads.items(), key=lambda kv: kv[1].get("time", 0), reverse=True)
    fails = {k: v for k, v in fails.items() if k not in seen}
    with open(STATE_FILE, "w") as f:
        json.dump({"seen": sorted(seen), "threads": dict(newest[:MAX_THREADS_KEPT]),
                   "fails": fails}, f, indent=1)


# ------------------------------------------------------------ text helpers --
def clean_text(text):
    """Tidy tweet text: keep line breaks, drop blank lines and extra spaces."""
    text = text.replace("\xa0", " ").replace("\r", "")
    text = re.sub(r"https?://[^/\s]+/t\.co/", "https://t.co/", text)   # mirror's copy of a t.co link
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def split_text(text, limit=290):
    """Split text into chunks of at most `limit` characters, filling each chunk
    as much as possible. Words and links are never cut."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        for i, word in enumerate(line.split()):
            sep = "" if not cur else ("\n" if i == 0 else " ")
            if cur and len(cur) + len(sep) + len(word) > limit:
                chunks.append(cur)
                cur = word
            else:
                cur += sep + word
    if cur:
        chunks.append(cur)
    return chunks


def parse_facets(text):
    """Make links and #hashtags clickable on Bluesky."""
    facets = []
    for m in re.finditer(r"(https?://\S+)|#\w+", text):
        if m.group(1):
            url = m.group(1).rstrip(".,;:!?)]}\"'…")      # no trailing punctuation
            end = m.start() + len(url)
            feature = {"$type": "app.bsky.richtext.facet#link", "uri": url}
        else:
            end = m.end()
            feature = {"$type": "app.bsky.richtext.facet#tag", "tag": m.group()[1:]}
        facets.append({
            "index": {"byteStart": len(text[:m.start()].encode()),
                      "byteEnd": len(text[:end].encode())},
            "features": [feature],
        })
    return facets


def describe(e):
    """Readable error text (some errors have an empty message on their own)."""
    resp = getattr(e, "response", None)
    return f"{type(e).__name__}: {e!r}" + (f" | response: {resp}" if resp is not None else "")


# ------------------------------------------------------------- fetch tweets --
def find_video(tweet_path):
    """Find a direct .mp4 link for a tweet: the mirror's page first, vxtwitter API as backup."""
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        html = requests.get(feed_host + tweet_path, headers=headers, timeout=15).text
        links = re.findall(r'''https://video\.twimg\.com/[^\s"'<>)]+\.mp4[^\s"'<>)]*''', html)
        if links:
            def pixels(link):               # prefer the biggest resolution
                m = re.search(r"/(\d+)x(\d+)/", link)
                return int(m[1]) * int(m[2]) if m else 0
            return max(links, key=pixels).replace("&amp;", "&")
    except Exception as e:
        print(f"Mirror video lookup failed: {e}")
    try:
        data = requests.get("https://api.vxtwitter.com" + tweet_path,
                            headers=headers, timeout=10).json()
        for media in data.get("media_extended", []):
            if media.get("type") == "video" and media.get("url"):
                return media["url"]
        for url in data.get("mediaURLs", []):
            if url.endswith(".mp4"):
                return url
    except Exception as e:
        print(f"vxtwitter video lookup failed: {e}")
    return None


def read_feed():
    """Download the RSS feed and return its entries. Each mirror in FEED_HOSTS is tried
    (a few times if it says "too many requests"). Stops with a clear message if none works."""
    global feed_host
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    for host in FEED_HOSTS:
        url = f"{host}/{TWITTER_USER}/rss"
        for attempt in range(1, 4):
            wait = 10 * attempt
            try:
                resp = requests.get(url, headers=headers, timeout=30)
            except Exception as e:
                print(f"Feed download failed ({url}): {describe(e)}")
            else:
                print(f"Feed {url} -> HTTP {resp.status_code} (attempt {attempt}/3)")
                entries = feedparser.parse(resp.content).entries if resp.ok else []
                if entries:
                    feed_host = host
                    return entries
                page = BeautifulSoup(resp.text, "html.parser").get_text(" ")
                print("No tweets. The page says:", " ".join(page.split())[:300])
                if resp.status_code not in (429, 500, 502, 503, 504):
                    break                                  # retrying won't help
                try:
                    wait = min(max(int(resp.headers.get("Retry-After", 0)), wait), 60)
                except ValueError:
                    pass
            if attempt < 3:
                print(f"Trying again in {wait}s")
                time.sleep(wait)
    raise SystemExit("Could not get any tweets from the feed (see the messages above)")


def fetch_tweets():
    tweets = []
    # In the feed, a tweet's own replies sit directly below it, so a self-reply
    # belongs to the last "main" tweet we passed.
    current_root = None

    for entry in read_feed()[:20]:
        title = entry.title.strip()
        low = title.lower()

        if low.startswith("rt by"):
            current_root = None
            continue
        if low.startswith("pinned:"):
            current_root = PINNED          # we don't repost the pinned tweet or its replies
            continue

        is_reply = low.startswith("r to ")
        if is_reply and not low.startswith(f"r to @{TWITTER_USER.lower()}"):
            continue                       # a reply to somebody else

        id_match = re.search(r"/status/(\d+)", entry.guid)
        tweet_id = id_match[1] if id_match else entry.guid     # same id whichever mirror we used
        parent_id = current_root if is_reply else None
        if not is_reply:
            current_root = tweet_id
        published = getattr(entry, "published_parsed", None)

        soup = BeautifulSoup(entry.description, "html.parser")
        for quote in soup.find_all("blockquote"):      # ignore quoted tweets
            quote.decompose()
        for br in soup.find_all("br"):
            br.replace_with("\n")
        # The title is truncated on this site, so use the full text instead
        first_p = soup.find("p")
        text = first_p.get_text() if first_p else re.sub(r"^R to @\w+:\s*", "", title)

        # A video shows up as a thumbnail linking to the tweet
        video_path = None
        for a in soup.find_all("a", href=True):
            img = a.find("img")
            if "/status/" in a["href"] and img and "amplify_video_thumb" in img.get("src", ""):
                href = a["href"].split("#")[0]
                video_path = "/" + href.split("/", 3)[3] if href.startswith("http") else href
                break

        # Images (skip video thumbnails, which sit inside a link to the tweet)
        images = []
        for img in soup.find_all("img"):
            link = img.find_parent("a")
            if link and "/status/" in link.get("href", ""):
                continue
            src = img.get("src", "").replace("https://nitter.net/pic/", "https://pbs.twimg.com/")
            src = requests.utils.unquote(src)
            if src and src not in images:
                images.append(src)

        tweets.append({
            "id": tweet_id,
            "text": clean_text(text),
            "images": images,
            "video_path": video_path,       # looked up (find_video) only if we post this tweet
            "is_reply": is_reply,
            "parent_id": parent_id,
            "time": calendar.timegm(published) if published else 0,
        })

    print(f"Fetched {len(tweets)} tweets")
    return tweets


# ------------------------------------------------------------------- video --
def probe_video(path):
    """Return (width, height, duration_seconds) for a local file or URL."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-show_entries", "format=duration",
             "-of", "json", path],
            capture_output=True, timeout=30).stdout
        info = json.loads(out)
        stream = (info.get("streams") or [{}])[0]
        return (int(stream.get("width", 0)), int(stream.get("height", 0)),
                float(info.get("format", {}).get("duration", 0)))
    except Exception as e:
        print(f"ffprobe failed: {e}")
        return 0, 0, 0


def download_video(url):
    """Download a video and convert it to a Bluesky-friendly mp4 (h264/aac),
    within the length, size and resolution limits. Returns a file path or None."""
    out = None
    try:
        out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        out.close()

        width, height, duration = probe_video(url)
        print(f"Source video: {width}x{height}, {duration:.1f}s")

        # Only the SHORT side is limited to 1080
        if width and height and min(width, height) > MAX_VIDEO_HEIGHT:
            scale = (f"scale={MAX_VIDEO_HEIGHT}:-2" if height > width
                     else f"scale=-2:{MAX_VIDEO_HEIGHT}")
        else:
            scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

        length = min(duration, MAX_VIDEO_SECONDS) if duration > 0 else MAX_VIDEO_SECONDS
        kbps = max(500, int(MAX_VIDEO_BYTES * 8 / length / 1000) - 128)

        result = subprocess.run([
            "ffmpeg", "-y", "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-i", url, "-t", str(length), "-vf", scale,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out.name,
        ], capture_output=True, timeout=1500)

        if result.returncode != 0:
            print(f"ffmpeg error: {result.stderr.decode()}")
        else:
            size = os.path.getsize(out.name)
            print(f"Downloaded and encoded: {size / 1024 / 1024:.1f} MB")
            if size <= MAX_VIDEO_BYTES:
                return out.name
            print("Video is over the size limit, skipping it")
    except Exception as e:
        print(f"Video download failed: {e}")
    if out and os.path.exists(out.name):
        os.unlink(out.name)
    return None


# ----------------------------------------------------------------- bluesky --
@functools.cache
def get_client():
    """Log in once per run and reuse the connection."""
    client = Client()
    client.login(BLUESKY_HANDLE, BLUESKY_PASSWORD)
    return client


def upload_blob(bsky, data, tries=3):
    """Upload a file to Bluesky, trying again if the connection hiccups."""
    for attempt in range(1, tries + 1):
        try:
            return bsky.upload_blob(data).blob
        except Exception as e:
            print(f"Upload attempt {attempt}/{tries} failed: {describe(e)}")
            if attempt == tries:
                raise
            time.sleep(5 * attempt)


def image_size(data):
    try:
        return Image.open(io.BytesIO(data)).size
    except Exception:
        return 0, 0


@functools.cache
def get_ocr():
    from rapidocr import RapidOCR      # only loaded when OCR is on; downloads its models on first use
    return RapidOCR()


def ocr_alt_text(data):
    """Read any text in the image and return it as alt text ('' if none found)."""
    if not OCR_ALT_TEXT:
        return ""
    try:
        import numpy as np
        img = np.array(Image.open(io.BytesIO(data)).convert("RGB"))[:, :, ::-1]   # RGB -> BGR
        result = get_ocr()(img)
        lines = [t.strip() for t, score in zip(result.txts or (), result.scores or ())
                 if score >= OCR_MIN_SCORE and sum(c.isalpha() for c in t) >= 3]   # skip icon/number junk
    except Exception as e:
        print(f"OCR skipped: {describe(e)}")
        return ""
    if not lines:
        return ""
    print(f"Alt text: {' '.join(lines)[:80]}")
    return ("Text in image: " + " ".join(lines))[:1000]


def build_embed(bsky, images, video_url):
    """Upload the media and return an embed for the first post (or None)."""
    if video_url:
        path = download_video(video_url)
        if not path:
            return None
        try:
            width, height, _ = probe_video(path)
            with open(path, "rb") as f:
                blob = upload_blob(bsky, f.read())
        except Exception as e:
            print(f"Video upload failed: {describe(e)}")
            return None
        finally:
            os.unlink(path)
        ratio = (models.AppBskyEmbedDefs.AspectRatio(width=width, height=height)
                 if width and height else None)
        return models.AppBskyEmbedVideo.Main(video=blob, aspect_ratio=ratio)

    uploaded = []
    for url in images[:4]:
        try:
            data = requests.get(url, timeout=10).content
            width, height = image_size(data)
            uploaded.append(models.AppBskyEmbedImages.Image(
                image=upload_blob(bsky, data), alt=ocr_alt_text(data),
                aspect_ratio=(models.AppBskyEmbedDefs.AspectRatio(width=width, height=height)
                              if width and height else None),
            ))
        except Exception as e:
            print(f"Image upload failed: {describe(e)}")
    return models.AppBskyEmbedImages.Main(images=uploaded) if uploaded else None


def wait_for_video(bsky, post_uri):
    """Wait until Bluesky has finished processing the video, so the next post
    doesn't go out while this one still shows 'processing'."""
    deadline = time.time() + VIDEO_WAIT_SECONDS
    while time.time() < deadline:
        try:
            posts = bsky.get_posts([post_uri]).posts
            playlist = getattr(posts[0].embed, "playlist", None) if posts else None
            if playlist:
                r = requests.get(playlist, timeout=10)
                if r.status_code == 200 and "#EXTM3U" in r.text:
                    print("Video is ready")
                    return
        except Exception as e:
            print(f"Checking video status: {describe(e)}")
        time.sleep(5)
    print("Gave up waiting for the video to finish processing, continuing")


# ------------------------------------------------- avoiding double posts ----
def recent_posts(limit=100):
    """Our own latest posts and replies (not reposts), newest first, as plain
    dicts. Returns None if the feed can't be read."""
    try:
        feed = get_client().get_author_feed(
            actor=BLUESKY_HANDLE, limit=limit, filter="posts_with_replies").feed
    except Exception as e:
        print(f"Could not read our own Bluesky feed: {describe(e)}")
        return None

    posts = []
    for item in feed:
        if getattr(item, "reason", None):           # a repost of someone else's post
            continue
        rec = item.post.record
        reply = getattr(rec, "reply", None)
        try:
            created = datetime.fromisoformat(rec.created_at.replace("Z", "+00:00")).timestamp()
        except Exception:
            created = 0
        posts.append({
            "uri": item.post.uri, "cid": item.post.cid,
            "text": getattr(rec, "text", "") or "",
            "parent": getattr(getattr(reply, "parent", None), "uri", None) if reply else None,
            "created": created,
        })
    return posts


def find_posted(posts, text, parent_uri=None, not_before=0):
    """Is this tweet already on Bluesky? Looks for a post with the same text under
    `parent_uri` (None = top level), made at/after `not_before` so an old tweet with
    the same wording isn't mistaken for it. Returns {"first": post, "last": post} or None."""
    chunks = split_text(text)
    first = next((p for p in posts or []
                  if p["text"] == chunks[0] and p["parent"] == parent_uri
                  and p["created"] >= not_before), None) if chunks else None
    if not first:
        return None
    last = first
    for chunk in chunks[1:]:                       # follow the rest of a long thread
        nxt = next((p for p in posts if p["parent"] == last["uri"] and p["text"] == chunk), None)
        if not nxt:
            break
        last = nxt
    return {"first": first, "last": last}


def ref(d):
    return models.ComAtprotoRepoStrongRef.Main(uri=d["uri"], cid=d["cid"])


def post_to_bluesky(text, images, video_url, thread=None):
    """Post `text` (split into a thread if long); media goes on the first post.
    If `thread` ({"root": ..., "last": ...}) is given, the posts are added to
    the end of it. Returns {"root": ..., "last": ...} or None if nothing posted."""
    root = parent = None
    posted = 0
    chunks = split_text(text) or [""]

    try:
        bsky = get_client()
        embed = build_embed(bsky, images, video_url)
        if thread:
            root, parent = ref(thread["root"]), ref(thread["last"])

        for i, chunk in enumerate(chunks):
            args = dict(
                text=chunk, facets=parse_facets(chunk) or None,
                reply_to=models.AppBskyFeedPost.ReplyRef(parent=parent, root=root) if parent else None,
            )
            use_embed = embed if i == 0 else None
            try:
                new = models.create_strong_ref(bsky.send_post(embed=use_embed, **args))
            except Exception as e:
                if not use_embed:
                    raise
                print(f"Post with media failed ({describe(e)}); checking, then trying without media")
                use_embed = None
                # it may have gone through anyway (e.g. a timeout) - don't post it twice
                hit = find_posted(recent_posts(20), chunk, parent.uri if parent else None,
                                  time.time() - 600)
                new = ref(hit["first"]) if hit else models.create_strong_ref(
                    bsky.send_post(**args))

            root = root or new
            parent = new
            posted += 1
            if use_embed and video_url:
                wait_for_video(bsky, new.uri)
            print(f"Posted part {i + 1}/{len(chunks)}:", chunk[:60])
    except Exception as e:
        print(f"Bluesky error: {describe(e)}")
        if posted:
            print(f"WARNING: only {posted} of {len(chunks)} parts were posted; the rest was dropped")

    if not posted:
        return None
    return {"root": {"uri": root.uri, "cid": root.cid},
            "last": {"uri": parent.uri, "cid": parent.cid}}


# -------------------------------------------------------------------- main --
def latest_thread(threads, before):
    """Fallback guess for a reply whose tweet the feed doesn't tell us:
    the newest thread posted before it."""
    older = [(t.get("time", 0), tid) for tid, t in threads.items() if t.get("time", 0) <= before]
    return max(older)[1] if older else None


def main():
    seen, threads, fails = load_state()
    tweets = fetch_tweets()

    if not seen and not threads:
        # First run: only post the newest FIRST_RUN_LIMIT tweets, plus their replies
        roots = sorted((t for t in tweets if not t["is_reply"]),
                       key=lambda t: t["time"], reverse=True)
        keep = {t["id"] for t in roots[:FIRST_RUN_LIMIT]}
        seen |= {t["id"] for t in tweets
                 if (t["parent_id"] if t["is_reply"] else t["id"]) not in keep}

    posts = None                      # our recent Bluesky posts, loaded once when needed
    pending = sorted((t for t in tweets if t["id"] not in seen), key=lambda t: t["time"])

    for tw in pending:                # oldest first, so a tweet goes out before its replies
        label = tw["text"][:60]
        if not (tw["text"] or tw["images"] or tw["video_path"]):
            seen.add(tw["id"])
            continue

        thread = None
        if tw["is_reply"]:
            pid = tw["parent_id"] or latest_thread(threads, tw["time"])
            if pid in (None, PINNED):
                print("Reply has no thread to attach to (or is under the pinned tweet), skipping:", label)
                seen.add(tw["id"])
                continue
            if pid not in threads:
                if pid in seen:
                    print("Reply's tweet was not reposted, skipping:", label)
                    seen.add(tw["id"])
                else:
                    print("Waiting for the original tweet before posting this reply")
                continue
            thread = threads[pid]

        # Make sure it isn't already on Bluesky (lost state file, overlapping runs, crash)
        hit = None
        if tw["text"]:
            if posts is None:
                posts = recent_posts() or []
            hit = find_posted(posts, tw["text"], thread["last"]["uri"] if thread else None,
                              tw["time"] - 60)

        if hit:
            print("Already on Bluesky, not posting again:", label)
            result = {
                "root": thread["root"] if thread else {"uri": hit["first"]["uri"], "cid": hit["first"]["cid"]},
                "last": {"uri": hit["last"]["uri"], "cid": hit["last"]["cid"]},
            }
        else:
            print("Posting reply:" if thread else "Reposting:", label)
            video_url = find_video(tw["video_path"]) if tw["video_path"] else None
            if tw["video_path"]:
                print(f"Video detected → {video_url}")
            result = post_to_bluesky(tw["text"], tw["images"], video_url, thread)

        if result:
            if thread:
                thread["last"] = result["last"]
            else:
                threads[tw["id"]] = {**result, "time": tw["time"]}
            seen.add(tw["id"])
        else:
            fails[tw["id"]] = fails.get(tw["id"], 0) + 1
            if fails[tw["id"]] < MAX_ATTEMPTS:
                # Stop here so newer tweets don't get posted ahead of this one
                print("Could not post this tweet. Stopping so the order stays correct; will retry next run.")
                save_state(seen, threads, fails)
                return
            print(f"Giving up on this tweet after {fails[tw['id']]} runs: {label}")
            seen.add(tw["id"])

        save_state(seen, threads, fails)   # save as we go, in case a later one crashes

    save_state(seen, threads, fails)


if __name__ == "__main__":
    main()