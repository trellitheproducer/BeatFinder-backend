"""
Posts routes: /api/posts
"""

from fastapi import APIRouter, Request, Depends, HTTPException, UploadFile, File, Form
from datetime import datetime
from pydantic import BaseModel
from typing import Optional, List
from bson import ObjectId
import os, httpx, hashlib, time as _time

from auth import get_current_user
from routes.notifications import create_notification
from routes.follower_notify import notify_post_to_followers

router = APIRouter()

CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "")
API_KEY    = os.getenv("CLOUDINARY_API_KEY", "")
API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "")


def _id():
    return str(ObjectId())


def _post_out(doc, liked=False):
    return {
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
        "liked":        liked,
        "createdAt":    doc.get("createdAt", datetime.utcnow()).isoformat()
                        if hasattr(doc.get("createdAt"), "isoformat") else str(doc.get("createdAt", "")),
    }


def _comment_out(doc):
    return {
        "id":        str(doc.get("_id", "")),
        "postId":    doc.get("postId", ""),
        "parentId":  doc.get("parentId", None),
        "username":  doc.get("username", ""),
        "avatarUrl": doc.get("avatarUrl", ""),
        "text":      doc.get("text", ""),
        "createdAt": doc.get("createdAt", datetime.utcnow()).isoformat()
                     if hasattr(doc.get("createdAt"), "isoformat") else "",
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


@router.get("/profile/{username}")
async def get_profile_posts(username: str, type: str = "status", request: Request = None):
    db   = request.app.state.db
    query = {"username": username}
    if type != "all":
        query["type"] = type
    docs = await db.posts.find(query).sort("createdAt", -1).to_list(50)
    return [_post_out(d) for d in docs]


@router.post("/status", status_code=201)
async def create_status(request: Request, user=Depends(get_current_user)):
    if user.get("plan") not in ("artist", "producer"):
        raise HTTPException(status_code=403, detail="Pro plan required")

    form = await request.form()
    text   = str(form.get("text", "")).strip()[:500]
    files  = form.getlist("images")

    if not text and not files:
        raise HTTPException(status_code=400, detail="Post needs text or an image")

    image_urls = []
    for i, f in enumerate(files[:3]):
        file_bytes   = await f.read()
        public_id    = f"post_{str(user['_id'])}_{int(_time.time())}_{i}"
        url = await upload_to_cloudinary(file_bytes, f.filename, f.content_type,
                                         "beatfinder/posts", public_id)
        image_urls.append(url)

    doc = {
        "_id":        _id(),
        "type":       "status",
        "text":       text,
        "images":     image_urls,
        "username":   user.get("username", ""),
        "avatarUrl":  user.get("avatarUrl", ""),
        "likeCount":  0,
        "commentCount": 0,
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
        "createdAt":  datetime.utcnow(),
    }
    await request.app.state.db.posts.insert_one(doc)
    # Notify followers — bundled "@user posted a new video"
    await notify_post_to_followers(request.app.state.db, user, doc["_id"], "video")
    return _post_out(doc)


@router.delete("/{post_id}")
async def delete_post(post_id: str, request: Request, user=Depends(get_current_user)):
    db     = request.app.state.db
    result = await db.posts.delete_one({"_id": post_id, "username": user.get("username")})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
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
