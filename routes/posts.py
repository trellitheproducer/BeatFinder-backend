"""
Posts routes: /api/posts
"""

from fastapi import APIRouter, Request, Depends, HTTPException, UploadFile, File, Form
from datetime import datetime, timedelta
from pydantic import BaseModel, HttpUrl
from typing import Optional, List
from bson import ObjectId
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
import os, httpx, hashlib, time as _time, html as _html_lib, re

from auth import get_current_user, get_admin_user
from routes.notifications import create_notification
from routes.follower_notify import notify_post_to_followers

router = APIRouter()

CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "")
API_KEY    = os.getenv("CLOUDINARY_API_KEY", "")
API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "")


def _id():
    return str(ObjectId())


async def _notify_about_post(db, to_user: str, from_user: str, kind: str, text: str, post_id: str):
    """Create a notification document that carries the post id, so the
    frontend can deep-link to the post when the user taps the notif.

    Field names match the schema used by routes/notifications.py
    (`toUser` / `fromUser` / `text` / `read` / `createdAt`) plus the
    extras already serialised by _notif_out there (`postId`, `postType`,
    `count`). We bypass create_notification() because its signature
    doesn't accept post_id — writing the doc ourselves keeps the
    deep-link field in place. Skips self-notifications, matching the
    existing helper's behaviour."""
    if not to_user or to_user == from_user:
        return
    try:
        await db.notifications.insert_one({
            "_id":       _id(),
            "toUser":    to_user,
            "fromUser":  from_user or "",
            "type":      kind,
            "text":      text,
            "postId":    post_id or "",
            "postType":  "",          # not relevant for like/comment/repost
            "count":     1,           # like/comment/repost notifs aren't bundled
            "read":      False,
            "createdAt": datetime.utcnow(),
        })
    except Exception:
        # Don't crash the request that triggered the notification — at
        # worst the user just doesn't get notified. We deliberately do
        # NOT fall back to create_notification here because it would
        # also fail with the same DB error and we'd lose the postId
        # silently. Better to log and move on.
        pass


# ── Open Graph metadata parser ────────────────────────────────────────────────
# Used for the in-post link previews. We pull og:title / og:description /
# og:image / og:site_name from the <head>; falling back to <title> and the
# first reasonable <meta name="description"> when the page doesn't expose
# Open Graph tags. Anything beyond the <body> tag is ignored to keep this
# fast on large pages.
class _OGParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.og = {}                # raw og:* attribute map
        self.title = ""
        self.description = ""
        # Fallback images discovered in the <head> — used when the page
        # has no og:image. Order matters; first non-empty wins.
        self.image_src   = ""       # <link rel="image_src" href="...">
        self.apple_icon  = ""       # <link rel="apple-touch-icon" href="...">
        self.icon        = ""       # <link rel="icon" href="...">
        self._in_title = False
        self._stopped = False

    def handle_starttag(self, tag, attrs):
        if self._stopped:
            return
        if tag == "body":
            # OG tags are always in <head>; bail out as soon as the body
            # opens to keep parsing time bounded on huge pages.
            self._stopped = True
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "link":
            a = {k.lower(): (v or "") for k, v in attrs}
            rel  = (a.get("rel", "") or "").lower()
            href = a.get("href", "")
            if not href:
                return
            if "image_src" in rel and not self.image_src:
                self.image_src = href
            elif "apple-touch-icon" in rel and not self.apple_icon:
                self.apple_icon = href
            elif rel == "icon" and not self.icon:
                self.icon = href
            return
        if tag != "meta":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        prop = a.get("property", "") or a.get("name", "")
        prop = prop.lower()
        content = a.get("content", "")
        if not content:
            return
        if prop.startswith("og:"):
            self.og[prop[3:]] = content
        elif prop == "twitter:title" and not self.og.get("title"):
            self.og["title"] = content
        elif prop == "twitter:description" and not self.og.get("description"):
            self.og["description"] = content
        elif prop in ("twitter:image", "twitter:image:src") and not self.og.get("image"):
            self.og["image"] = content
        elif prop == "description" and not self.description:
            self.description = content
        elif prop == "thumbnail" and not self.og.get("image"):
            # Some YouTube-adjacent pages use <meta name="thumbnail">
            self.og["image"] = content

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()


def _absolute_url(base: str, maybe_relative: str) -> str:
    if not maybe_relative:
        return ""
    try:
        return urljoin(base, maybe_relative)
    except Exception:
        return maybe_relative


def _short(s: str, n: int) -> str:
    if not s:
        return ""
    s = _html_lib.unescape(s).strip()
    return s[:n]


# ── YouTube helpers ───────────────────────────────────────────────────────────
# YouTube's short links (youtu.be/{id}) serve a sparse page without proper
# Open Graph tags, so we normalize to the long form before fetching. We also
# keep the extracted video ID around so we can fall back to img.youtube.com
# for the thumbnail if the fetched page somehow lacks og:image.
_YOUTUBE_HOSTS = (
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "youtu.be", "music.youtube.com",
)

def _youtube_video_id(url: str) -> str:
    """Return the YouTube video id if this URL points to a YouTube video,
    else empty string. Handles: youtu.be/{id}, youtube.com/watch?v={id},
    youtube.com/shorts/{id}, youtube.com/embed/{id}."""
    try:
        u = urlparse(url)
    except Exception:
        return ""
    host = (u.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        return ""
    # youtu.be/{id}
    if host == "youtu.be":
        seg = u.path.lstrip("/").split("/", 1)[0]
        return seg if re.match(r"^[A-Za-z0-9_-]{6,20}$", seg) else ""
    # youtube.com/watch?v={id}
    if u.path == "/watch":
        from urllib.parse import parse_qs
        q = parse_qs(u.query)
        vid = (q.get("v") or [""])[0]
        return vid if re.match(r"^[A-Za-z0-9_-]{6,20}$", vid) else ""
    # /shorts/{id} or /embed/{id} or /v/{id}
    m = re.match(r"^/(?:shorts|embed|v)/([A-Za-z0-9_-]{6,20})", u.path)
    if m:
        return m.group(1)
    return ""


def _youtube_thumbnail(video_id: str) -> str:
    """Return the highest-quality YouTube thumbnail URL for a video id.
    Synchronous fallback — picks `hqdefault.jpg` which is available for
    EVERY YouTube video without needing a network probe. Used when we
    can't do an async probe (e.g. inline construction)."""
    if not video_id:
        return ""
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"


async def _best_youtube_thumbnail(video_id: str) -> str:
    """Async variant: probe YouTube's thumbnail CDN for the highest
    available quality. Order is maxresdefault → sddefault → hqdefault.
    `maxresdefault` only exists for HD videos that someone watched in
    HD, but when it exists it's by far the nicest preview. hqdefault is
    the universal fallback — guaranteed to exist for every video.

    Returns the URL of the highest-resolution image that actually
    exists. Probes use HEAD requests with a 3s timeout, so worst-case
    cost is ~9s for an exotic video — but maxresdefault hits typically
    succeed in <500ms."""
    if not video_id:
        return ""
    candidates = [
        f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
        f"https://img.youtube.com/vi/{video_id}/sddefault.jpg",
        f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
    ]
    # YouTube returns 404 (sometimes 200 + a tiny placeholder) for
    # missing thumbnails. We HEAD then check content-length: real
    # thumbnails are 10KB+; the placeholder is ~1KB.
    try:
        async with httpx.AsyncClient(timeout=3.0, follow_redirects=True) as client:
            for url in candidates:
                try:
                    r = await client.head(url)
                    if r.status_code == 200:
                        # Some YT regions/edges return a 120x90 placeholder
                        # gray image at 200 OK for missing thumbnails;
                        # filter those out by content-length.
                        cl = r.headers.get("content-length")
                        if cl and cl.isdigit() and int(cl) < 5000:
                            continue
                        return url
                except Exception:
                    continue
    except Exception:
        pass
    # If every probe failed (network down, CDN issue, etc), still hand
    # back hqdefault — it's the safest universal fallback and the
    # placeholder gradient will render if even THAT 404s in the browser.
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"


def _normalize_url(url: str) -> str:
    """Rewrite known short-form URLs to a form that serves richer OG tags.
    Currently: youtu.be/{id} → youtube.com/watch?v={id}."""
    try:
        u = urlparse(url)
    except Exception:
        return url
    host = (u.hostname or "").lower()
    if host == "youtu.be":
        vid = u.path.lstrip("/").split("/", 1)[0]
        if re.match(r"^[A-Za-z0-9_-]{6,20}$", vid):
            return f"https://www.youtube.com/watch?v={vid}"
    return url


# Regex used to scrape URLs out of post text bodies (both the live /status
# endpoint and the admin backfill). Matches http/https URLs, stops at
# whitespace and common trailing punctuation.
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)


def _extract_first_url(text: str) -> str:
    if not text:
        return ""
    m = _URL_RE.search(text)
    if not m:
        return ""
    return m.group(0).rstrip(".,;!?)")


async def _fetch_link_preview(url: str) -> dict:
    """Fetch a URL and return a dict of OG metadata. Defensive — never
    raises; returns {} on any failure. Caps the response body so a
    malicious site can't OOM us by serving 500MB of HTML."""
    # Capture the original URL before normalization so the stored linkUrl
    # still points where the user actually typed (clicking the preview
    # should send them to the URL they pasted, not our rewritten one).
    original_url = url
    url = _normalize_url(url)
    yt_id = _youtube_video_id(url)

    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return {}
        # Reject private / loopback hosts so users can't probe our internal
        # network through the preview endpoint (SSRF). Crude but effective.
        host = (parsed.hostname or "").lower()
        if (host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")
                or host.endswith(".local")
                or host.startswith("10.")
                or host.startswith("192.168.")
                or host.startswith("169.254.")
                or any(host.startswith(f"172.{i}.") for i in range(16, 32))):
            return {}
    except Exception:
        return {}

    headers = {
        # Some sites (Twitter/X, LinkedIn) serve much richer OG metadata
        # when they think they're being scraped by a real preview bot.
        "User-Agent": "Mozilla/5.0 (compatible; BeatFinderBot/1.0; +https://beatfinder.co.uk)",
        "Accept":     "text/html,application/xhtml+xml",
    }
    body = ""
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, max_redirects=4) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code != 200:
                    body = ""
                else:
                    # Only parse text/html responses; spare us downloading
                    # PDFs / images / videos that happen to be at the URL.
                    ct = (resp.headers.get("content-type") or "").lower()
                    if "html" in ct:
                        # Read up to ~512KB — enough for the <head> on any sane
                        # page, and a hard ceiling against runaway downloads.
                        buf = bytearray()
                        async for chunk in resp.aiter_bytes(16 * 1024):
                            buf.extend(chunk)
                            if len(buf) >= 512 * 1024:
                                break
                        # Detect charset from headers, fall back to utf-8
                        charset = "utf-8"
                        m = re.search(r"charset=([\w-]+)", ct)
                        if m:
                            charset = m.group(1)
                        try:
                            body = bytes(buf).decode(charset, errors="ignore")
                        except Exception:
                            body = bytes(buf).decode("utf-8", errors="ignore")
    except Exception:
        body = ""

    p = _OGParser()
    if body:
        try:
            p.feed(body)
        except Exception:
            pass

    title       = p.og.get("title") or p.title or ""
    description = p.og.get("description") or p.description or ""
    image       = p.og.get("image") or ""
    site_name   = p.og.get("site_name") or ""

    # Fallback chain for the image — og:image → twitter:image (already in
    # p.og.image via the parser) → <link rel="image_src"> → apple-touch-icon
    # → favicon. We resolve relative URLs against the page URL.
    if not image:
        image = p.image_src or p.apple_icon or p.icon or ""

    # Resolve relative og:image URLs against the final URL
    image = _absolute_url(url, image)

    # YouTube override: for known YouTube videos, ALWAYS use the direct
    # video thumbnail URL — not whatever og:image came back from the page.
    # YouTube sometimes serves a generic brand logo as og:image (for age-
    # restricted, region-blocked, or freshly-uploaded videos) which would
    # otherwise produce the giant red "YouTube" placeholder. The direct
    # img.youtube.com URL is reliable and gives us the actual video frame.
    if yt_id:
        better = await _best_youtube_thumbnail(yt_id)
        if better:
            image = better
        if not title or title.lower() == "youtube":
            # OG title was missing or just "YouTube" (the brand) — try to
            # find the video title elsewhere on the page.
            if body and not title:
                m = re.search(r'<title>([^<]+)</title>', body, re.IGNORECASE)
                if m:
                    title = m.group(1).replace(" - YouTube", "").strip()
            if not title:
                title = "YouTube Video"
        if not site_name:
            site_name = "YouTube"

    # Default siteName to the hostname when site doesn't provide one — gives
    # the preview card something to show as the small uppercase header.
    if not site_name:
        try:
            site_name = (urlparse(original_url).hostname or "").replace("www.", "")
        except Exception:
            site_name = ""

    return {
        # Return the ORIGINAL url the user typed; the rewrite was just so
        # we'd get richer metadata back from the host.
        "url":         original_url,
        "title":       _short(title, 200),
        "description": _short(description, 400),
        "image":       image[:600] if image else "",
        "siteName":    _short(site_name, 80),
    }


def _iso_utc(dt) -> str:
    """Append Z so JavaScript treats stored naive-UTC datetimes as UTC.
    Same helper as routes/auth.py — duplicated here so this file stands
    alone without cross-route imports."""
    if not dt:
        return ""
    if hasattr(dt, "isoformat"):
        s = dt.isoformat()
        return s if s.endswith("Z") or "+" in s else s + "Z"
    s = str(dt)
    if not s:
        return ""
    return s if s.endswith("Z") or "+" in s[10:] else s + "Z"


def _post_out(doc, liked=False, reposted=False, original_post=None, author_plan=""):
    """Serialise a post doc. If this doc is a repost (has repost_of set),
    `original_post` should be the already-serialised dict of the original;
    we attach it under "original_post" so the client can render the
    original's content with the reposter's identity above it.

    `author_plan` is the author's current plan ("artist" | "producer" |
    "free"); used by the frontend to decide whether to show the
    verified tick next to their name. Optional — defaults to empty
    string (frontend then treats them as non-verified)."""
    out = {
        "id":           str(doc.get("_id", "")),
        "type":         doc.get("type", "status"),
        "text":         doc.get("text", ""),
        "images":       doc.get("images", []),
        "spotifyUrl":   doc.get("spotifyUrl", ""),
        "embedUrl":     doc.get("embedUrl", ""),
        "title":        doc.get("title", ""),
        "artist":       doc.get("artist", ""),
        "thumbnail":    doc.get("thumbnail", ""),
        "videoUrl":     doc.get("videoUrl", ""),
        "caption":      doc.get("caption", ""),
        "username":     doc.get("username", ""),
        "avatarUrl":    doc.get("avatarUrl", ""),
        # Author's plan — frontend reads this to decide whether to
        # render the verified tick next to the username.
        "plan":         author_plan or "",
        "likeCount":    doc.get("likeCount", 0),
        "commentCount": doc.get("commentCount", 0),
        "repostCount":  doc.get("repostCount", 0),
        "liked":        liked,
        "reposted":     reposted,
        "repost_of":    doc.get("repost_of"),
        "original_post": original_post,
        # Link preview fields — populated when /status is given a link_url.
        # Stored on the post doc so the preview persists even if the
        # remote site changes its OG tags or goes down later.
        "linkUrl":         doc.get("linkUrl", ""),
        "linkTitle":       doc.get("linkTitle", ""),
        "linkDescription": doc.get("linkDescription", ""),
        "linkImage":       doc.get("linkImage", ""),
        "linkSiteName":    doc.get("linkSiteName", ""),
        "createdAt":    _iso_utc(doc.get("createdAt", datetime.utcnow())),
    }
    return out


def _comment_out(doc):
    return {
        "id":        str(doc.get("_id", "")),
        "postId":    doc.get("postId", ""),
        "parentId":  doc.get("parentId", None),
        "username":  doc.get("username", ""),
        "avatarUrl": doc.get("avatarUrl", ""),
        "text":      doc.get("text", ""),
        "createdAt": _iso_utc(doc.get("createdAt", datetime.utcnow())),
    }


async def upload_to_cloudinary(file_bytes, filename, content_type, folder, public_id):
    timestamp = int(_time.time())
    to_sign   = f"folder={folder}&public_id={public_id}&timestamp={timestamp}" + API_SECRET
    signature = hashlib.sha256(to_sign.encode()).hexdigest()
    upload_url = f"https://api.cloudinary.com/v1_1/{CLOUD_NAME}/image/upload"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(upload_url,
            data={"api_key": API_KEY, "timestamp": timestamp,
                  "folder": folder, "public_id": public_id, "signature": signature},
            files={"file": (filename, file_bytes, content_type)})
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Image upload failed")
    return resp.json().get("secure_url", "")


async def _hydrate_originals(db, docs: list) -> dict:
    """For any docs that are reposts (have repost_of), fetch the original
    post documents in a single batched query and return a map keyed by
    original post id (string). Used so the client gets the original
    content inline without an extra round-trip per repost."""
    original_ids = [d.get("repost_of") for d in docs if d.get("repost_of")]
    if not original_ids:
        return {}
    # Posts use string _ids (created via str(ObjectId()))
    originals = await db.posts.find({"_id": {"$in": original_ids}}).to_list(len(original_ids))
    return {str(o["_id"]): o for o in originals}


@router.get("/profile/{username}")
async def get_profile_posts(username: str, type: str = "status", request: Request = None):
    db = request.app.state.db
    query = {"username": username}
    if type != "all":
        query["type"] = type
    docs = await db.posts.find(query).sort("createdAt", -1).to_list(50)

    # If any are reposts, pull the original docs so we can inline them
    originals_map = await _hydrate_originals(db, docs)

    # Bulk-fetch plans for every author appearing in this result set —
    # both the profile owner (wrappers) AND any reposted-author usernames
    # (originals can be by other users). One query, then we look up by
    # username when serialising each post.
    usernames_set = set()
    for d in docs:
        if d.get("username"):
            usernames_set.add(d["username"])
    for o in originals_map.values():
        if o.get("username"):
            usernames_set.add(o["username"])
    plan_by_username = {}
    if usernames_set:
        plan_docs = await db.users.find(
            {"username": {"$in": list(usernames_set)}},
            {"username": 1, "plan": 1},
        ).to_list(length=len(usernames_set) + 10)
        for u in plan_docs:
            if u.get("username"):
                plan_by_username[u["username"]] = u.get("plan", "")

    out = []
    for d in docs:
        original_post = None
        if d.get("repost_of"):
            orig = originals_map.get(d["repost_of"])
            if orig:
                original_post = _post_out(
                    orig,
                    author_plan=plan_by_username.get(orig.get("username", ""), ""),
                )
        out.append(_post_out(
            d,
            original_post=original_post,
            author_plan=plan_by_username.get(d.get("username", ""), ""),
        ))
    return out


@router.post("/status", status_code=201)
async def create_status(request: Request, user=Depends(get_current_user)):
    if user.get("plan") not in ("artist", "producer"):
        raise HTTPException(status_code=403, detail="Pro plan required")

    form = await request.form()
    text   = str(form.get("text", "")).strip()[:500]
    files  = form.getlist("images")
    # Optional URL the client extracted from the text. We re-validate and
    # re-fetch on the server so a malicious client can't fabricate a fake
    # preview with someone else's branding.
    link_url = str(form.get("link_url", "")).strip()[:2048]
    # If the client didn't pre-extract a URL but one is sitting in the
    # text body, pick it up server-side so EVERY post with a link gets
    # a preview — even if the post was created via a client that didn't
    # implement URL detection.
    if not link_url and text:
        link_url = _extract_first_url(text)[:2048]

    if not text and not files:
        raise HTTPException(status_code=400, detail="Post needs text or an image")

    image_urls = []
    for i, f in enumerate(files[:3]):
        file_bytes   = await f.read()
        public_id    = f"post_{str(user['_id'])}_{int(_time.time())}_{i}"
        url = await upload_to_cloudinary(file_bytes, f.filename, f.content_type,
                                         "beatfinder/posts", public_id)
        image_urls.append(url)

    # Fetch link preview metadata if a URL was supplied. We persist it on
    # the post doc so the preview keeps rendering even if the remote
    # changes their OG tags or the site goes down later.
    link_meta = {}
    if link_url:
        link_meta = await _fetch_link_preview(link_url) or {}

    doc = {
        "_id":        _id(),
        "type":       "status",
        "text":       text,
        "images":     image_urls,
        "username":   user.get("username", ""),
        "avatarUrl":  user.get("avatarUrl", ""),
        "likeCount":  0,
        "commentCount": 0,
        "repostCount": 0,
        "linkUrl":         link_meta.get("url", "") or (link_url if link_url else ""),
        "linkTitle":       link_meta.get("title", ""),
        "linkDescription": link_meta.get("description", ""),
        "linkImage":       link_meta.get("image", ""),
        "linkSiteName":    link_meta.get("siteName", ""),
        "createdAt":  datetime.utcnow(),
    }
    await request.app.state.db.posts.insert_one(doc)
    # Notify followers — bundled "@user posted an update" / "@user posted N updates"
    # If images attached, treat as image post (more interesting at-a-glance).
    await notify_post_to_followers(
        request.app.state.db, user, doc["_id"],
        "image" if image_urls else "status",
    )
    return _post_out(doc)


# ── Live link preview (used by the composer to show a preview as the user types) ──
class LinkPreviewBody(BaseModel):
    url: str

@router.post("/link-preview")
async def link_preview(body: LinkPreviewBody, user=Depends(get_current_user)):
    """Fetch and return Open Graph metadata for an arbitrary URL. Auth-only
    so anonymous traffic can't use us as a free SSRF proxy. Returns the
    same shape as the linkXxx fields persisted on /status posts."""
    url = (body.url or "").strip()
    if not url or not url.lower().startswith(("http://", "https://")):
        return {}
    if len(url) > 2048:
        return {}
    return await _fetch_link_preview(url) or {}


@router.post("/admin/backfill-link-previews")
async def backfill_link_previews(
    request: Request,
    limit: int = 500,
    force: bool = False,
    user=Depends(get_admin_user),
):
    """Admin endpoint — re-fetch link preview metadata for posts.

    Behavior:
      • Walks the posts collection looking for status posts whose text
        contains a URL or whose linkUrl field is set.
      • For each one, fetches the URL's OG metadata and writes the five
        linkXxx fields back onto the post doc.
      • Skips posts that already have a linkImage set unless ?force=true
        is passed. This makes the endpoint idempotent: you can run it
        repeatedly and it only touches posts that still need it.
      • Caps processing at `limit` posts per call so a single request
        can't run for hours. Default 500 is plenty for any small/medium
        deployment. Hit it multiple times if you have more than that.

    Returns:
      {
        scanned: int,     # how many candidate posts we looked at
        fetched: int,     # how many we actually re-fetched (network)
        updated: int,     # how many got new metadata written
        skipped: int,     # how many were skipped (already had image)
        failed:  int,     # fetches that returned nothing usable
      }
    """
    db = request.app.state.db
    # Candidate query: any post that has a linkUrl already, OR has a URL
    # somewhere in its text body. We can't do regex on text in a portable
    # way without a Mongo regex op, so we pull text posts and filter in
    # Python — cheap enough for the volumes we're at.
    cursor = db.posts.find({
        "$or": [
            {"linkUrl": {"$exists": True, "$ne": ""}},
            {"text":    {"$regex": r"https?://", "$options": "i"}},
        ],
    }).limit(max(1, min(int(limit), 2000)))

    scanned = 0
    fetched = 0
    updated = 0
    skipped = 0
    failed  = 0

    async for post in cursor:
        scanned += 1
        # Decide which URL to fetch for this post: prefer stored linkUrl,
        # else extract the first URL from the text.
        url = (post.get("linkUrl") or "").strip()
        if not url:
            url = _extract_first_url(post.get("text", ""))
        if not url:
            skipped += 1
            continue
        # Idempotency: if we already have a thumbnail and the caller
        # didn't pass force=true, leave it alone.
        if not force and post.get("linkImage"):
            skipped += 1
            continue

        fetched += 1
        meta = await _fetch_link_preview(url) or {}
        # Even on total failure the parser tries to give us a hostname
        # for siteName, so check whether we actually have something
        # better than what's already on the doc before writing.
        new_image       = meta.get("image", "")
        new_title       = meta.get("title", "")
        new_description = meta.get("description", "")
        new_site_name   = meta.get("siteName", "")
        # If we still have no image at all, count it as failed but
        # still update the title/description/siteName so the placeholder
        # at least has a hostname strip on it.
        if not new_image and not new_title and not new_description:
            failed += 1
            # Still set linkUrl + siteName so the placeholder renders
            await db.posts.update_one(
                {"_id": post["_id"]},
                {"$set": {
                    "linkUrl":      url,
                    "linkSiteName": new_site_name,
                }},
            )
            continue

        await db.posts.update_one(
            {"_id": post["_id"]},
            {"$set": {
                "linkUrl":         url,
                "linkTitle":       new_title,
                "linkDescription": new_description,
                "linkImage":       new_image,
                "linkSiteName":    new_site_name,
            }},
        )
        updated += 1

    return {
        "scanned": scanned,
        "fetched": fetched,
        "updated": updated,
        "skipped": skipped,
        "failed":  failed,
    }


class MusicPostBody(BaseModel):
    spotifyUrl: str
    caption:    Optional[str] = ""

@router.post("/music", status_code=201)
async def create_music_post(body: MusicPostBody, request: Request, user=Depends(get_current_user)):
    if user.get("plan") not in ("artist", "producer"):
        raise HTTPException(status_code=403, detail="Pro plan required")

    url = body.spotifyUrl.strip()
    content_type = None; content_id = None
    for ct in ["track", "album", "playlist", "episode"]:
        if f"spotify.com/{ct}/" in url:
            content_type = ct
            content_id   = url.split(f"spotify.com/{ct}/")[1].split("?")[0].split("/")[0]
            break
        if f"spotify:{ct}:" in url:
            content_type = ct
            content_id   = url.split(f"spotify:{ct}:")[1].split("?")[0]
            break

    if not content_type or not content_id:
        raise HTTPException(status_code=400, detail="Invalid Spotify URL")

    embed_url   = f"https://open.spotify.com/embed/{content_type}/{content_id}"
    spotify_url = f"https://open.spotify.com/{content_type}/{content_id}"
    title = ""; thumbnail = ""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get("https://open.spotify.com/oembed", params={"url": spotify_url})
            if resp.status_code == 200:
                data = resp.json()
                title     = data.get("title", "")
                thumbnail = data.get("thumbnail_url", "")
    except Exception:
        pass

    doc = {
        "_id":        _id(),
        "type":       "music",
        "spotifyUrl": spotify_url,
        "embedUrl":   embed_url,
        "title":      title,
        "thumbnail":  thumbnail,
        "caption":    (body.caption or "").strip()[:300],
        "username":   user.get("username", ""),
        "avatarUrl":  user.get("avatarUrl", ""),
        "likeCount":  0,
        "commentCount": 0,
        "repostCount": 0,
        "createdAt":  datetime.utcnow(),
    }
    await request.app.state.db.posts.insert_one(doc)
    # Notify followers — bundled "@user shared a new track"
    await notify_post_to_followers(request.app.state.db, user, doc["_id"], "music")
    return _post_out(doc)


@router.post("/video", status_code=201)
async def create_video_post(request: Request, user=Depends(get_current_user)):
    if user.get("plan") not in ("artist", "producer"):
        raise HTTPException(status_code=403, detail="Pro plan required")

    form    = await request.form()
    caption = str(form.get("caption", "")).strip()[:300]
    f       = form.get("file")
    if not f:
        raise HTTPException(status_code=400, detail="No video file")

    file_bytes = await f.read()
    if len(file_bytes) > 100 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Video too large - max 100MB")

    timestamp  = int(_time.time())
    public_id  = f"video_{str(user['_id'])}_{timestamp}"
    folder     = "beatfinder/videos"
    to_sign    = f"folder={folder}&public_id={public_id}&timestamp={timestamp}" + API_SECRET
    signature  = hashlib.sha256(to_sign.encode()).hexdigest()

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"https://api.cloudinary.com/v1_1/{CLOUD_NAME}/video/upload",
            data={"api_key": API_KEY, "timestamp": timestamp, "folder": folder,
                  "public_id": public_id, "signature": signature},
            files={"file": (f.filename, file_bytes, f.content_type)})

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Video upload failed")

    video_url = resp.json().get("secure_url", "")
    doc = {
        "_id":        _id(),
        "type":       "video",
        "videoUrl":   video_url,
        "caption":    caption,
        "username":   user.get("username", ""),
        "avatarUrl":  user.get("avatarUrl", ""),
        "likeCount":  0,
        "commentCount": 0,
        "repostCount": 0,
        "createdAt":  datetime.utcnow(),
    }
    await request.app.state.db.posts.insert_one(doc)
    # Notify followers — bundled "@user posted a new video"
    await notify_post_to_followers(request.app.state.db, user, doc["_id"], "video")
    return _post_out(doc)


@router.delete("/{post_id}")
async def delete_post(post_id: str, request: Request, user=Depends(get_current_user)):
    db     = request.app.state.db
    # First find the post — needed so reposters can delete their own repost docs.
    post = await db.posts.find_one({"_id": post_id, "username": user.get("username")})
    if not post:
        raise HTTPException(status_code=404, detail="Not found")

    result = await db.posts.delete_one({"_id": post_id, "username": user.get("username")})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Not found")

    # If this was a repost, decrement the original post's repost count
    if post.get("repost_of"):
        await db.posts.update_one(
            {"_id": post["repost_of"]},
            {"$inc": {"repostCount": -1}}
        )
    else:
        # Original post deleted — clean up associated reposts so they don't
        # render as orphans pointing at a missing original.
        await db.posts.delete_many({"repost_of": post_id})

    await db.post_comments.delete_many({"postId": post_id})
    await db.post_likes.delete_many({"postId": post_id})
    return {"deleted": True}


@router.post("/{post_id}/like")
async def like_post(post_id: str, request: Request, user=Depends(get_current_user)):
    db       = request.app.state.db
    username = user.get("username")
    existing = await db.post_likes.find_one({"postId": post_id, "username": username})
    if existing:
        await db.post_likes.delete_one({"_id": existing["_id"]})
        await db.posts.update_one({"_id": post_id}, {"$inc": {"likeCount": -1}})
        return {"liked": False}
    await db.post_likes.insert_one({"postId": post_id, "username": username, "createdAt": datetime.utcnow()})
    await db.posts.update_one({"_id": post_id}, {"$inc": {"likeCount": 1}})
    # Notify post owner
    post_doc = await db.posts.find_one({"_id": post_id})
    if post_doc:
        await _notify_about_post(
            db, post_doc.get("username"), username, "like",
            f"@{username} liked your post", post_id,
        )
    return {"liked": True}


@router.get("/{post_id}/liked")
async def check_liked(post_id: str, request: Request, user=Depends(get_current_user)):
    db  = request.app.state.db
    hit = await db.post_likes.find_one({"postId": post_id, "username": user.get("username")})
    return {"liked": bool(hit)}


# ── Repost ───────────────────────────────────────────────────────────────────
# A repost is a thin post document with `repost_of: <original_post_id>` owned
# by the reposting user. It carries no text/media of its own — the client
# resolves the original via the `original_post` field populated by the
# server. The original's `repostCount` counter is incremented on POST and
# decremented on DELETE. We only allow one repost per user per post (toggle
# semantics) and we never let users repost their own posts.
@router.post("/{post_id}/repost", status_code=201)
async def repost_post(post_id: str, request: Request, user=Depends(get_current_user)):
    db       = request.app.state.db
    username = user.get("username") or ""
    if not username:
        raise HTTPException(status_code=400, detail="Set a username before reposting")

    original = await db.posts.find_one({"_id": post_id})
    if not original:
        raise HTTPException(status_code=404, detail="Original post not found")
    # Can't repost your own post — defensive (frontend disables the button too)
    if original.get("username") == username:
        raise HTTPException(status_code=400, detail="You can't repost your own post")
    # Can't repost a repost — always point at the underlying original. If the
    # client somehow sent us a repost id, resolve to the real original.
    underlying_id = original.get("repost_of") or post_id
    if original.get("repost_of"):
        original = await db.posts.find_one({"_id": underlying_id})
        if not original:
            raise HTTPException(status_code=404, detail="Original post not found")

    # Already reposted? Toggle semantics — return the existing repost id.
    existing = await db.posts.find_one({"username": username, "repost_of": underlying_id})
    if existing:
        return {"reposted": True, "id": str(existing["_id"]), "alreadyReposted": True}

    doc = {
        "_id":         _id(),
        "type":        original.get("type", "status"),
        "repost_of":   underlying_id,
        "username":    username,
        "avatarUrl":   user.get("avatarUrl", ""),
        "createdAt":   datetime.utcnow(),
        # Repost docs don't carry their own like/comment/repost counts —
        # all engagement targets the underlying original. We still set the
        # fields so listing queries don't choke on None.
        "likeCount":   0,
        "commentCount": 0,
        "repostCount": 0,
    }
    await db.posts.insert_one(doc)
    await db.posts.update_one({"_id": underlying_id}, {"$inc": {"repostCount": 1}})

    # Notify the original poster
    await _notify_about_post(
        db, original.get("username"), username, "repost",
        f"@{username} reposted your post", underlying_id,
    )
    return {"reposted": True, "id": doc["_id"]}


@router.delete("/{post_id}/repost")
async def unrepost_post(post_id: str, request: Request, user=Depends(get_current_user)):
    """Un-repost: removes the current user's repost of `post_id` (where
    `post_id` is the underlying ORIGINAL post id). Decrements the original
    post's repostCount. Idempotent — returns success even if there was
    nothing to remove."""
    db       = request.app.state.db
    username = user.get("username") or ""
    # `post_id` may be the original or (legacy) a repost id; normalise.
    target = await db.posts.find_one({"_id": post_id})
    underlying_id = post_id
    if target and target.get("repost_of"):
        underlying_id = target["repost_of"]

    result = await db.posts.delete_one({"username": username, "repost_of": underlying_id})
    if result.deleted_count:
        await db.posts.update_one(
            {"_id": underlying_id},
            {"$inc": {"repostCount": -1}}
        )
    return {"reposted": False}


@router.get("/{post_id}/reposted")
async def check_reposted(post_id: str, request: Request, user=Depends(get_current_user)):
    """Did the current user repost this post? Accepts either the original
    post id or a repost id (the latter is normalised to its underlying
    original first)."""
    db       = request.app.state.db
    username = user.get("username") or ""
    target = await db.posts.find_one({"_id": post_id})
    underlying_id = post_id
    if target and target.get("repost_of"):
        underlying_id = target["repost_of"]
    hit = await db.posts.find_one({"username": username, "repost_of": underlying_id})
    return {"reposted": bool(hit)}


class CommentBody(BaseModel):
    text:     str
    parentId: Optional[str] = None

@router.get("/{post_id}/comments")
async def get_comments(post_id: str, request: Request):
    db   = request.app.state.db
    docs = await db.post_comments.find({"postId": post_id}).sort("createdAt", 1).to_list(200)
    return [_comment_out(d) for d in docs]


@router.post("/{post_id}/comments", status_code=201)
async def add_comment(post_id: str, body: CommentBody, request: Request, user=Depends(get_current_user)):
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="Comment cannot be empty")
    db  = request.app.state.db
    doc = {
        "_id":      _id(),
        "postId":   post_id,
        "parentId": body.parentId,
        "username": user.get("username", ""),
        "avatarUrl":user.get("avatarUrl", ""),
        "text":     body.text.strip()[:500],
        "createdAt":datetime.utcnow(),
    }
    await db.post_comments.insert_one(doc)
    await db.posts.update_one({"_id": post_id}, {"$inc": {"commentCount": 1}})
    # Notify post owner
    post_doc = await db.posts.find_one({"_id": post_id})
    if post_doc:
        await _notify_about_post(
            db, post_doc.get("username"), user.get("username"), "comment",
            f"@{user.get('username')} commented: \"{body.text.strip()[:60]}\"",
            post_id,
        )
    return _comment_out(doc)


@router.delete("/{post_id}/comments/{comment_id}")
async def delete_comment(post_id: str, comment_id: str, request: Request, user=Depends(get_current_user)):
    db     = request.app.state.db
    result = await db.post_comments.delete_one({"_id": comment_id, "username": user.get("username")})
    if result.deleted_count:
        await db.posts.update_one({"_id": post_id}, {"$inc": {"commentCount": -1}})
    return {"deleted": True}
