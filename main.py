import os
import json
import re
import subprocess
import tempfile
import requests
import feedparser
from bs4 import BeautifulSoup
from atproto import Client, models

NITTER_RSS = "https://xitter.cf/InfinityNikkiEN/rss"
BLUESKY_HANDLE = os.environ["BLUESKY_HANDLE"]
BLUESKY_PASSWORD = os.environ["BLUESKY_PASSWORD"]
STATE_FILE = "seen_ids.json"


def load_seen():
    try:
        return set(json.load(open(STATE_FILE)))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen):
    with open(STATE_FILE, "w") as f:
        json.dump(list(seen), f)


def fetch_media_from_vxtwitter(tweet_path):
    """
    Use the api.vxtwitter.com JSON API to get media URLs for a tweet.
    Returns (video_url, image_urls) where video_url may be None.
    """
    api_url = "https://api.vxtwitter.com" + tweet_path
    try:
        print(f"Fetching media from vxtwitter API: {api_url}")
        resp = requests.get(
            api_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        data = resp.json()

        media_urls = data.get("mediaURLs", [])
        video_url = None
        image_urls = []

        for url in media_urls:
            if url.endswith(".mp4") or "video" in url:
                if video_url is None:
                    video_url = url
            else:
                image_urls.append(url)

        # Also check the structured media array if present
        for media in data.get("media_extended", []):
            mtype = media.get("type", "")
            url = media.get("url", "")
            if mtype == "video" and video_url is None:
                video_url = url
            elif mtype == "image" and url not in image_urls:
                image_urls.append(url)

        return video_url, image_urls

    except Exception as e:
        print(f"vxtwitter API fetch failed: {e}")

    return None, []


def fetch_tweets():
    feed = feedparser.parse(NITTER_RSS)
    tweets = []
    
    for entry in feed.entries[:10]:
        if entry.title.startswith(("RT by", "R to", "Pinned:")):
            continue

        tweet_id = entry.guid

        soup = BeautifulSoup(entry.description, "html.parser")

        # Ignore quoted tweets so only this tweet's own content is used
        for quote in soup.find_all("blockquote"):
            quote.decompose()

        # Title is truncated on this site, so use the full text from the description
        for br in soup.find_all("br"):
            br.replace_with("\n")
        first_p = soup.find("p")
        text = first_p.get_text() if first_p else entry.title
        text = text.replace("https://xitter.cf/t.co/", "https://t.co/").strip()

        images = []
        video_url = None
        tweet_path = None
        has_video = False

        # Detect video using RSS pattern
        for a in soup.find_all("a", href=True):
            if "/status/" in a["href"]:
                href = a["href"].split("#")[0]

                # Normalize to path only
                if href.startswith("http"):
                    # Remove domain (nitter or twitter)
                    tweet_path = "/" + href.split("/", 3)[3]
                else:
                    tweet_path = href
                img = a.find("img")
                if img:
                    src = img.get("src", "")
                    if "amplify_video_thumb" in src:
                        has_video = True
                        break

        if has_video and tweet_path:
            video_url, api_images = fetch_media_from_vxtwitter(tweet_path)
            if api_images and not images:
                images = api_images
            print(f"Video detected → {video_url}")
        elif tweet_path and not images:
            # Try API for images too (catches cases where RSS missed them)
            _, api_images = fetch_media_from_vxtwitter(tweet_path)
            if api_images:
                images = api_images
                print(f"Images from API → {len(images)}")
        else:
            print("No video in tweet")

        # Extract images (skip video thumbnails)
        for img in soup.find_all("img"):
            parent = img.find_parent("a")
            if parent and "/status/" in parent.get("href", ""):
                continue

            src = img.get("src", "")
            src = src.replace("https://nitter.net/pic/", "https://pbs.twimg.com/")
            src = requests.utils.unquote(src)
            images.append(src)

        tweets.append({
            "id": tweet_id,
            "text": text,
            "images": images,
            "video_url": video_url
        })

    print(f"Fetched {len(tweets)} tweets")
    return tweets


BSKY_MAX_DURATION = 180        # 3 minutes in seconds
BSKY_MAX_BYTES    = 100 * 1024 * 1024  # 100 MB
BSKY_MAX_HEIGHT   = 1080


def probe_video(path):
    """Return (width, height, duration_seconds) for a local video file."""
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
        width    = int(streams[0].get("width",  0)) if streams else 0
        height   = int(streams[0].get("height", 0)) if streams else 0
        duration = float(fmt.get("duration", 0))
        return width, height, duration
    except Exception as e:
        print(f"ffprobe failed: {e}")
        return 0, 0, 0


def download_video(url):
    """
    Download and transcode video to a Bluesky-compatible MP4 (libx264/aac).
    Always re-encodes to ensure a clean MP4 regardless of source format (HLS,
    MPEG-TS, etc.) and to enforce the 1080p/3min/100MB limits.
    Returns path to a ready-to-upload .mp4, or None on failure.
    """
    try:
        out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        out.close()

        # Probe the source directly (works for HLS/HTTP URLs too)
        width, height, duration = probe_video(url)
        print(f"Source video: {width}x{height}, {duration:.1f}s")

        # "1080p" means the SHORT side is at most 1080.
        # For vertical video (height > width), the short side is width.
        # For horizontal video (width > height), the short side is height.
        # scale=-2:1080 fixes height → wrong for portrait (squashes width to ~607)
        # scale=1080:-2 fixes width → correct for portrait
        short_side = min(width, height) if width and height else 0
        is_vertical = height > width

        if short_side > BSKY_MAX_HEIGHT:
            if is_vertical:
                # Limit width (short side) to 1080, height scales proportionally
                scale_filter = f"scale={BSKY_MAX_HEIGHT}:-2"
                print(f"Vertical video — downscaling width to {BSKY_MAX_HEIGHT}px")
            else:
                # Limit height (short side) to 1080, width scales proportionally
                scale_filter = f"scale=-2:{BSKY_MAX_HEIGHT}"
                print(f"Horizontal video — downscaling height to {BSKY_MAX_HEIGHT}px")
        else:
            # Already within limits — just ensure even dimensions for libx264
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
            "-preset", "fast",
            "-crf", "23",
            "-maxrate", f"{safe_video_kbps}k",
            "-bufsize", f"{safe_video_kbps * 2}k",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            out.name
        ]

        result = subprocess.run(cmd, capture_output=True, timeout=300)
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


def parse_facets(text):
    facets = []

    for match in re.finditer(r'https?://[^\s]+', text):
        start = len(text[:match.start()].encode("utf-8"))
        end = len(text[:match.end()].encode("utf-8"))
        facets.append({
            "index": {"byteStart": start, "byteEnd": end},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": match.group()}]
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


def get_image_dimensions(data):
    """
    Extract (width, height) from raw image bytes without any extra dependencies.
    Supports JPEG and PNG, which covers all Twitter/Bluesky images.
    Returns (0, 0) if dimensions can't be determined.
    """
    try:
        # PNG: 8-byte signature, then IHDR chunk: 4-byte length, "IHDR", width, height
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            import struct
            w, h = struct.unpack('>II', data[16:24])
            return w, h

        # JPEG: scan for SOF markers (0xFFC0–0xFFC3, 0xFFC5–0xFFC7, etc.)
        if data[:2] == b'\xff\xd8':
            import struct
            i = 2
            while i < len(data) - 8:
                if data[i] != 0xff:
                    break
                marker = data[i + 1]
                # SOF markers that contain dimensions
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                               0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack('>HH', data[i + 5:i + 9])
                    return w, h
                # Skip this segment
                seg_len = struct.unpack('>H', data[i + 2:i + 4])[0]
                i += 2 + seg_len
    except Exception:
        pass
    return 0, 0


def upload_video_to_bsky(bsky, video_path):
    """
    Upload a video blob directly to the PDS via uploadBlob (simple method).
    Returns (blob, width, height) on success, or (None, 0, 0) on failure.
    """
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
        return None, 0, 0
def split_text(text, limit=290):
    """Split text into chunks of at most `limit` characters.
    Breaks at line breaks first, then at spaces, so links are never cut."""
    chunks = []
    current = ""

    for line in text.split("\n"):
        # Break an over-long line into word-sized pieces
        if len(line) > limit:
            pieces, buf = [], ""
            for word in line.split(" "):
                candidate = f"{buf} {word}" if buf else word
                if len(candidate) > limit and buf:
                    pieces.append(buf)
                    buf = word
                else:
                    buf = candidate
            pieces.append(buf)
        else:
            pieces = [line]

        for piece in pieces:
            candidate = f"{current}\n{piece}" if current else piece
            if len(candidate) > limit and current:
                chunks.append(current.strip())
                current = piece
            else:
                current = candidate

    if current.strip():
        chunks.append(current.strip())
    return chunks

def post_to_bluesky(text, images, video_url):
    try:
        bsky = Client()
        bsky.login(BLUESKY_HANDLE, BLUESKY_PASSWORD)

        embed = None

        if video_url:
            video_path = download_video(video_url)
            if video_path:
                blob, width, height = upload_video_to_bsky(bsky, video_path)
                os.unlink(video_path)
                if blob:
                    embed_kwargs = {"video": blob}
                    if width and height:
                        embed_kwargs["aspect_ratio"] = models.AppBskyEmbedDefs.AspectRatio(
                            width=width, height=height
                        )
                    embed = models.AppBskyEmbedVideo.Main(**embed_kwargs)
                else:
                    print("Video upload to Bluesky failed")
            else:
                print("Video download failed")

        elif images:
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
                embed = models.AppBskyEmbedImages.Main(
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

        # Split into thread parts if the text is longer than Bluesky allows
        chunks = split_text(text) or [""]
        root = None
        parent = None

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
            print(f"Posted part {i + 1}/{len(chunks)}:", chunk[:60])

    except Exception as e:
        print(f"Bluesky error: {e}")

def main():
    seen = load_seen()
    tweets = fetch_tweets()

    if not seen:
        for tw in tweets:
            seen.add(tw["id"])
        save_seen(seen)
        print("First run: marked existing tweets as seen, nothing posted.")
        return
    
    for tw in reversed(tweets):
        if tw["id"] in seen:
            continue

        print("Reposting:", tw["text"][:80])
        post_to_bluesky(tw["text"], tw["images"], tw["video_url"])
        seen.add(tw["id"])

    save_seen(seen)


if __name__ == "__main__":
    main()
