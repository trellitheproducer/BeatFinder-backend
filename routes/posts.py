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

from auth import get_current_user
from routes.notifications import create_notification
from routes.follower_notify import notify_post_to_followers

router = APIRouter()

CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "")
API_KEY    = os.getenv("CLOUDINARY_API_KEY", "")
API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "")


def _id():
    return str(ObjectId())


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
        elif prop == "twitter:image" and not self.og.get("image"):
            self.og["image"] = content
        elif prop == "description" and not self.description:
            self.description = content

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


async def _fetch_link_preview(url: str) -> dict:
    """Fetch a URL and return a dict of OG metadata. Defensive — never
    raises; returns {} on any failure. Caps the response body so a
    malicious site can't OOM us by serving 500MB of HTML."""
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
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, max_redirects=4) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code != 200:
                    return {}
                # Only parse text/html responses; spare us downloading
                # PDFs / images / videos that happen to be at the URL.
                ct = (resp.headers.get("content-type") or "").lower()
                if "html" not in ct:
                    return {}
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
        return {}

    p = _OGParser()
    try:
        p.feed(body)
    except Exception:
        pass

    title       = p.og.get("title") or p.title or ""
    description = p.og.get("description") or p.description or ""
    image       = p.og.get("image") or ""
    site_name   = p.og.get("site_name") or ""

    # Resolve relative og:image URLs against the final URL
    image = _absolute_url(url, image)

    # Default siteName to the hostname when site doesn't provide one — gives
    # the preview card something to show as the small uppercase header.
    if not site_name:
        try:
            site_name = (urlparse(url).hostname or "").replace("www.", "")
        except Exception:
            site_name = ""

    return {
        "url":         url,
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


def _post_out(doc, liked=False, reposted=False, original_post=None):
    """Serialise a post doc. If this doc is a repost (has repost_of set),
    `original_post` should be the already-serialised dict of the original;
    we attach it under "original_post" so the client can render the
    original's content with the reposter's identity above it."""
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

    out = []
    for d in docs:
        original_post = None
        if d.get("repost_of"):
            orig = originals_map.get(d["repost_of"])
            if orig:
                original_post = _post_out(orig)
        out.append(_post_out(d, original_post=original_post))
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
        await create_notification(db, post_doc.get("username"), username, "like",
            f"@{username} liked your post")
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
    await create_notification(
        db, original.get("username"), username, "repost",
        f"@{username} reposted your post"
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
        await create_notification(db, post_doc.get("username"), user.get("username"), "comment",
            f"@{user.get('username')} commented: \"{body.text.strip()[:60]}\"")
    return _comment_out(doc)


@router.delete("/{post_id}/comments/{comment_id}")
async def delete_comment(post_id: str, comment_id: str, request: Request, user=Depends(get_current_user)):
    db     = request.app.state.db
    result = await db.post_comments.delete_one({"_id": comment_id, "username": user.get("username")})
    if result.deleted_count:
        await db.posts.update_one({"_id": post_id}, {"$inc": {"commentCount": -1}})
    return {"deleted": True}
