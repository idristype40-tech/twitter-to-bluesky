import os
import json
import re
import calendar
import subprocess
import tempfile
import requests
import feedparser
from bs4 import BeautifulSoup
from atproto import Client, models

# ---------------------------------------------------------------- settings --
TWITTER_USER = "InfinityNikkiEN"
FEED_URL = f"https://xitter.cf/{TWITTER_USER}/rss"

BLUESKY_HANDLE = os.environ["BLUESKY_HANDLE"]
BLUESKY_PASSWORD = os.environ["BLUESKY_PASSWORD"]

STATE_FILE = "seen_ids.json"
MAX_THREADS_KEPT = 50          # how many past threads to remember for replies
PINNED = "PINNED"              # marker: the reply sits under the pinned tweet

# When seen_ids.json is empty, the bot would post every tweet in the feed at
# once. With True (recommended) it only remembers them and posts nothing the
# first time. Set to False if you WANT it to post the current backlog.
SKIP_EXISTING_ON_FIRST_RUN = False
FIRST_RUN_LIMIT = 10

BSKY_MAX_DURATION = 600                 # 10 minutes in seconds
BSKY_MAX_BYTES = 250 * 1024 * 1024      # a little under Bluesky's 300 MB cap
BSKY_MAX_HEIGHT = 1080


# ------------------------------------------------------------------- state --
def load_state():
    """Return (set of seen tweet ids, dict of threads we've posted)."""
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = []

    if isinstance(data, list):          # old format: just a list of ids
        return set(data), {}
    return set(data.get("seen", [])), data.get("threads", {})


def save_state(seen, threads):
    newest = sorted(
        threads.items(), key=lambda kv: kv[1].get("time", 0), reverse=True
    )[:MAX_THREADS_KEPT]
    with open(STATE_FILE, "w") as f:
        json.dump({"seen": sorted(seen), "threads": dict(newest)}, f, indent=1)


# ------------------------------------------------------------- text helpers --
def clean_text(text):
    """Tidy tweet text: keep line breaks, drop blank lines and extra spaces."""
    text = text.replace("\xa0", " ").replace("\r", "")
    text = text.replace("https://xitter.cf/t.co/", "https://t.co/")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def split_text(text, limit=290, min_fill=150):
    """Split text into chunks of at most `limit` characters.
    Fills each chunk as much as possible. Prefers to break at a line break
    (if the chunk is already at least `min_fill` long), otherwise breaks
    between words. Words and links are never cut."""
    tokens = []
    for line in text.split("\n"):
        first = True
        for word in line.split():
            if not tokens:
                sep = ""
            elif first:
                sep = "\n"
            else:
                sep = " "
            tokens.append((sep, word))
            first = False

    chunks = []
    cur = ""
    last_nl = -1  # position of the most recent line break inside cur

    for sep, word in tokens:
        piece = (sep if cur else "") + word
        if cur and len(cur) + len(piece) > limit:
            if last_nl >= min_fill:
                chunks.append(cur[:last_nl].strip())
                cur = cur[last_nl + 1:] + piece
            else:
                chunks.append(cur.strip())
                cur = word
            last_nl = cur.rfind("\n")
        else:
            if cur and sep == "\n":
                last_nl = len(cur)
            cur += piece

    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def parse_facets(text):
    facets = []

    for match in re.finditer(r'https?://[^\s]+', text):
        url = match.group().rstrip('.,;:!?)]}"\'…')   # don't include trailing punctuation
        start = len(text[:match.start()].encode("utf-8"))
        end = start + len(url.encode("utf-8"))
        facets.append({
            "index": {"byteStart": start, "byteEnd": end},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}]
        })

    for match in re.finditer(r'#\w+', text):
        tag = match.group()[1:]
        start = len(text[:match.start()].encode("utf-8"))
        end = len(text[:match.end()].encode("utf-8"))
        facets.append({
            "index": {"byteStart": start, "byteEnd": end},
            "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag}]
        })

    return facets


# ------------------------------------------------------- thread matching ----
def norm(text):
    """Lowercase letters/digits only, so '♾ Foo | Bar' matches '♾️ Foo | Bar'."""
    return re.sub(r'[\W_]+', '', text.lower())


def first_line_key(text):
    return norm(text.split("\n")[0]) if text else ""


def find_parent_thread(threads, reply):
    """FALLBACK guess, used only when the feed position doesn't tell us which
    tweet a reply belongs to (e.g. the reply is the very first item in the feed).

      1. prefer the thread whose first line matches the reply's text
      2. otherwise use the most recent thread posted before the reply.
    Returns the thread's id, or None if there is no earlier thread.
    """
    candidates = [
        (tid, t) for tid, t in threads.items()
        if t.get("time", 0) <= reply["time"]
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[1].get("time", 0), reverse=True)

    rkey = norm(reply["text"])
    if len(rkey) >= 8:
        for tid, t in candidates:
            key = t.get("key", "")
            if len(key) >= 8 and (
                rkey == key or rkey.startswith(key) or key.startswith(rkey)
            ):
                return tid

    return candidates[0][0]


# ------------------------------------------------------------ media lookup --
def fetch_video_from_xitter(tweet_path):
    """Find the direct .mp4 link on the xitter.cf tweet page."""
    page_url = "https://xitter.cf" + tweet_path
    try:
        print(f"Looking for video on {page_url}")
        resp = requests.get(page_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        links = re.findall(
            r'''https://video\.twimg\.com/[^\s"'<>)]+\.mp4[^\s"'<>)]*''', resp.text
        )
        if not links:
            print("No .mp4 link found on tweet page")
            return None

        def pixels(link):
            m = re.search(r'/(\d+)x(\d+)/', link)
            return int(m.group(1)) * int(m.group(2)) if m else 0

        best = max(links, key=pixels).replace("&amp;", "&")
        print(f"Found video: {best}")
        return best
    except Exception as e:
        print(f"xitter video lookup failed: {e}")
        return None


def fetch_video_from_vxtwitter(tweet_path):
    """Fallback: ask the api.vxtwitter.com JSON API for the video URL."""
    api_url = "https://api.vxtwitter.com" + tweet_path
    try:
        print(f"Fetching media from vxtwitter API: {api_url}")
        resp = requests.get(api_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = resp.json()

        for media in data.get("media_extended", []):
            if media.get("type") == "video" and media.get("url"):
                return media["url"]
        for url in data.get("mediaURLs", []):
            if url.endswith(".mp4") or "video" in url:
                return url
    except Exception as e:
        print(f"vxtwitter API fetch failed: {e}")
    return None


# ---------------------------------------------------------- fetch tweets ----
def fetch_tweets():
    feed = feedparser.parse(FEED_URL)
    tweets = []

    # In the feed, a tweet's own replies sit directly below it. So while we walk
    # down the feed we remember the last "main" tweet, and any self-reply we
    # meet belongs to that tweet.
    current_root = None

    for entry in feed.entries[:20]:
        title = entry.title.strip()
        low = title.lower()

        if low.startswith("rt by"):
            current_root = None
            continue
        if low.startswith("pinned:"):
            current_root = PINNED      # we don't repost the pinned tweet or its replies
            continue

        is_reply = low.startswith("r to ")
        if is_reply and not low.startswith(f"r to @{TWITTER_USER.lower()}"):
            continue  # a reply to somebody else, not part of our own thread

        tweet_id = entry.guid
        parent_id = None
        if is_reply:
            parent_id = current_root
        else:
            current_root = tweet_id
        published = getattr(entry, "published_parsed", None)
        tweet_time = calendar.timegm(published) if published else 0

        soup = BeautifulSoup(entry.description, "html.parser")

        # Ignore quoted tweets so only this tweet's own content is used
        for quote in soup.find_all("blockquote"):
            quote.decompose()

        # The title is truncated on this site, so use the full text instead
        for br in soup.find_all("br"):
            br.replace_with("\n")
        first_p = soup.find("p")
        if first_p:
            text = first_p.get_text()
        else:
            text = re.sub(r"^R to @\w+:\s*", "", title)
        text = clean_text(text)

        # Look for a video (the feed shows a thumbnail linking to the tweet)
        video_url = None
        tweet_path = None
        has_video = False

        for a in soup.find_all("a", href=True):
            if "/status/" in a["href"]:
                href = a["href"].split("#")[0]
                if href.startswith("http"):
                    tweet_path = "/" + href.split("/", 3)[3]
                else:
                    tweet_path = href
                img = a.find("img")
                if img and "amplify_video_thumb" in img.get("src", ""):
                    has_video = True
                    break

        if has_video and tweet_path:
            video_url = (
                fetch_video_from_xitter(tweet_path)
                or fetch_video_from_vxtwitter(tweet_path)
            )
            print(f"Video detected → {video_url}")

        # Images (skip video thumbnails, which sit inside a link to the tweet)
        images = []
        for img in soup.find_all("img"):
            parent = img.find_parent("a")
            if parent and "/status/" in parent.get("href", ""):
                continue
            src = img.get("src", "")
            src = src.replace("https://nitter.net/pic/", "https://pbs.twimg.com/")
            src = requests.utils.unquote(src)
            if src and src not in images:
                images.append(src)

        tweets.append({
            "id": tweet_id,
            "text": text,
            "images": images,
            "video_url": video_url,
            "is_reply": is_reply,
            "parent_id": parent_id,
            "time": tweet_time,
        })

    print(f"Fetched {len(tweets)} tweets")
    return tweets


# ------------------------------------------------------------------- video --
def probe_video(path):
    """Return (width, height, duration_seconds) for a local file or URL."""
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-show_entries", "format=duration",
            "-of", "json",
            path
        ], capture_output=True, timeout=30)
        info = json.loads(result.stdout)
        streams = info.get("streams", [{}])
        fmt = info.get("format", {})
        width = int(streams[0].get("width", 0)) if streams else 0
        height = int(streams[0].get("height", 0)) if streams else 0
        duration = float(fmt.get("duration", 0))
        return width, height, duration
    except Exception as e:
        print(f"ffprobe failed: {e}")
        return 0, 0, 0


def download_video(url):
    """
    Download and transcode a video to a Bluesky-compatible MP4 (libx264/aac),
    enforcing the length/size/resolution limits.
    Returns the path to a ready-to-upload .mp4, or None on failure.
    """
    try:
        out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        out.close()

        width, height, duration = probe_video(url)
        print(f"Source video: {width}x{height}, {duration:.1f}s")

        # "1080p" means the SHORT side is at most 1080.
        short_side = min(width, height) if width and height else 0
        is_vertical = height > width

        if short_side > BSKY_MAX_HEIGHT:
            if is_vertical:
                scale_filter = f"scale={BSKY_MAX_HEIGHT}:-2"
                print(f"Vertical video — downscaling width to {BSKY_MAX_HEIGHT}px")
            else:
                scale_filter = f"scale=-2:{BSKY_MAX_HEIGHT}"
                print(f"Horizontal video — downscaling height to {BSKY_MAX_HEIGHT}px")
        else:
            scale_filter = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

        trim_duration = min(duration, BSKY_MAX_DURATION) if duration > 0 else BSKY_MAX_DURATION
        safe_video_kbps = max(500, int((BSKY_MAX_BYTES * 8 / trim_duration) / 1000) - 128)

        cmd = [
            "ffmpeg", "-y",
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-i", url,
            "-t", str(trim_duration),
            "-vf", scale_filter,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-maxrate", f"{safe_video_kbps}k",
            "-bufsize", f"{safe_video_kbps * 2}k",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            out.name
        ]

        result = subprocess.run(cmd, capture_output=True, timeout=1500)
        if result.returncode == 0:
            size = os.path.getsize(out.name)
            print(f"Downloaded and encoded: {size / 1024 / 1024:.1f} MB")
            return out.name
        else:
            print(f"ffmpeg error: {result.stderr.decode()}")
            os.unlink(out.name)
            return None

    except Exception as e:
        print(f"Download failed: {e}")
        return None


def get_image_dimensions(data):
    """Read (width, height) from raw PNG/JPEG bytes. Returns (0, 0) if unknown."""
    try:
        import struct
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            w, h = struct.unpack('>II', data[16:24])
            return w, h

        if data[:2] == b'\xff\xd8':
            i = 2
            while i < len(data) - 8:
                if data[i] != 0xff:
                    break
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack('>HH', data[i + 5:i + 9])
                    return w, h
                seg_len = struct.unpack('>H', data[i + 2:i + 4])[0]
                i += 2 + seg_len
    except Exception:
        pass
    return 0, 0


def upload_video_to_bsky(bsky, video_path):
    """Upload a video blob. Returns (blob, width, height) or (None, 0, 0)."""
    try:
        width, height, _ = probe_video(video_path)
        with open(video_path, "rb") as f:
            video_data = f.read()
        upload = bsky.upload_blob(video_data)
        print(f"Uploaded video ({len(video_data) / 1024 / 1024:.1f} MB)")
        return upload.blob, width, height
    except Exception as e:
        print(f"Video upload error: {e}")
        return None, 0, 0


# ----------------------------------------------------------------- bluesky --
_client = None


def get_client():
    """Log in once per run and reuse the connection."""
    global _client
    if _client is None:
        client = Client()
        client.login(BLUESKY_HANDLE, BLUESKY_PASSWORD)
        _client = client
    return _client


def build_embed(bsky, images, video_url):
    """Upload the media and return an embed for the first post (or None)."""
    if video_url:
        video_path = download_video(video_url)
        if not video_path:
            print("Video download failed")
            return None
        blob, width, height = upload_video_to_bsky(bsky, video_path)
        os.unlink(video_path)
        if not blob:
            print("Video upload to Bluesky failed")
            return None
        kwargs = {"video": blob}
        if width and height:
            kwargs["aspect_ratio"] = models.AppBskyEmbedDefs.AspectRatio(
                width=width, height=height
            )
        return models.AppBskyEmbedVideo.Main(**kwargs)

    if images:
        image_blobs = []
        for url in images[:4]:
            try:
                resp = requests.get(url, timeout=10)
                img_width, img_height = get_image_dimensions(resp.content)
                blob = bsky.upload_blob(resp.content)
                image_blobs.append((blob.blob, img_width, img_height))
            except Exception as e:
                print(f"Image upload failed: {e}")

        if image_blobs:
            return models.AppBskyEmbedImages.Main(
                images=[
                    models.AppBskyEmbedImages.Image(
                        image=blob,
                        alt="",
                        aspect_ratio=models.AppBskyEmbedDefs.AspectRatio(
                            width=w, height=h
                        ) if w and h else None,
                    )
                    for blob, w, h in image_blobs
                ]
            )
    return None


def post_to_bluesky(text, images, video_url, thread=None):
    """
    Post `text` (split into a thread if it is long). Media goes on the first
    post. If `thread` is given ({"root": ref, "last": ref}), the new posts are
    added to the end of that existing thread.

    Returns {"root": ref, "last": ref} describing what was posted, or None if
    nothing could be posted.
    """
    root = parent = None
    posted = 0

    try:
        bsky = get_client()
        embed = build_embed(bsky, images, video_url)

        if thread:
            root = models.ComAtprotoRepoStrongRef.Main(
                uri=thread["root"]["uri"], cid=thread["root"]["cid"]
            )
            parent = models.ComAtprotoRepoStrongRef.Main(
                uri=thread["last"]["uri"], cid=thread["last"]["cid"]
            )

        chunks = split_text(text) or [""]
        for i, chunk in enumerate(chunks):
            reply_to = None
            if parent:
                reply_to = models.AppBskyFeedPost.ReplyRef(parent=parent, root=root)

            facets = parse_facets(chunk)
            response = bsky.send_post(
                text=chunk,
                facets=facets if facets else None,
                embed=embed if i == 0 else None,   # media only on the first post
                reply_to=reply_to,
            )

            ref = models.create_strong_ref(response)
            if root is None:
                root = ref
            parent = ref
            posted += 1
            print(f"Posted part {i + 1}/{len(chunks)}:", chunk[:60])

    except Exception as e:
        print(f"Bluesky error: {e}")

    if not posted:
        return None
    return {
        "root": {"uri": root.uri, "cid": root.cid},
        "last": {"uri": parent.uri, "cid": parent.cid},
    }


# -------------------------------------------------------------------- main --
def main():
    seen, threads = load_state()
    tweets = fetch_tweets()

    if not seen and not threads and SKIP_EXISTING_ON_FIRST_RUN:
        for tw in tweets:
            seen.add(tw["id"])
        save_state(seen, threads)
        print("First run: marked existing tweets as seen, nothing posted.")
        return

    # Oldest first, so a tweet is always posted before its replies
    pending = [t for t in tweets if t["id"] not in seen]
    pending.sort(key=lambda t: t["time"])

    for tw in pending:
        if not tw["text"] and not tw["images"] and not tw["video_url"]:
            seen.add(tw["id"])
            continue

        if tw["is_reply"]:
            parent_id = tw["parent_id"]

            if parent_id == PINNED:
                print("Reply to the pinned tweet, skipping:", tw["text"][:60])
                seen.add(tw["id"])
                continue

            if parent_id is None:               # position unknown: make a best guess
                parent_id = find_parent_thread(threads, tw)
                if parent_id is None:
                    print("Reply has no earlier thread to attach to, skipping:", tw["text"][:60])
                    seen.add(tw["id"])
                    continue
            elif parent_id not in threads:
                if parent_id in seen:           # its tweet was never mirrored by us
                    print("Reply's tweet was not reposted, skipping:", tw["text"][:60])
                    seen.add(tw["id"])
                else:                           # its tweet hasn't gone out yet: retry next run
                    print("Waiting for the original tweet before posting this reply")
                continue

            print(f"Adding reply to the end of thread {parent_id}:", tw["text"][:60])
            thread = threads[parent_id]
            result = post_to_bluesky(
                tw["text"], tw["images"], tw["video_url"],
                thread={"root": thread["root"], "last": thread["last"]},
            )
            if result:
                thread["last"] = result["last"]
                seen.add(tw["id"])
        else:
            print("Reposting:", tw["text"][:80])
            result = post_to_bluesky(tw["text"], tw["images"], tw["video_url"])
            if result:
                threads[tw["id"]] = {
                    "root": result["root"],
                    "last": result["last"],
                    "key": first_line_key(tw["text"]),
                    "time": tw["time"],
                }
                seen.add(tw["id"])

        save_state(seen, threads)   # save as we go, in case a later one crashes

    save_state(seen, threads)


if __name__ == "__main__":
    main()