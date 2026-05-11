"""
Content routes: /api/content
Handles music tracks (Spotify links) and video uploads.
Likes and comments with threading.
"""

from fastapi import APIRouter, Request, Depends, HTTPException, UploadFile, File
from datetime import datetime
from pydantic import BaseModel
from typing import Optional, List
from bson import ObjectId
import os, httpx, hashlib, time as _time

from auth import get_current_user

router = APIRouter()

CLOUD_NAME  = os.getenv("CLOUDINARY_CLOUD_NAME", "")
API_KEY     = os.getenv("CLOUDINARY_API_KEY", "")
API_SECRET  = os.getenv("CLOUDINARY_API_SECRET", "")


def _id():
    return str(ObjectId())


def _track_out(doc):
    return {
        "id":           str(doc.get("_id", "")),
        "type":         doc.get("type", "music"),
        "spotifyUrl":   doc.get("spotifyUrl", ""),
        "embedUrl":     doc.get("embedUrl", ""),
        "title":        doc.get("title", ""),
        "artist":       doc.get("artist", ""),
        "thumbnail":    doc.get("thumbnail", ""),
        "videoUrl":     doc.get("videoUrl", ""),
        "caption":      doc.get("caption", ""),
        "username":     doc.get("username", ""),
        "likeCount":    doc.get("likeCount", 0),
        "commentCount": doc.get("commentCount", 0),
        "createdAt":    doc.get("createdAt", datetime.utcnow()).isoformat()
                        if hasattr(doc.get("createdAt"), "isoformat") else str(doc.get("createdAt", "")),
    }


def _comment_out(doc):
    return {
        "id":        str(doc.get("_id", "")),
        "contentId": doc.get("contentId", ""),
        "parentId":  doc.get("parentId", None),
        "username":  doc.get("username", ""),
        "avatarUrl": doc.get("avatarUrl", ""),
        "text":      doc.get("text", ""),
        "likeCount": doc.get("likeCount", 0),
        "createdAt": doc.get("createdAt", datetime.utcnow()).isoformat()
                     if hasattr(doc.get("createdAt"), "isoformat") else "",
    }


# ── List content for a user profile ──────────────────────────────
@router.get("/profile/{username}")
async def get_profile_content(username: str, type: str, request: Request):
    db   = request.app.state.db
    docs = await db.content.find(
        {"username": username, "type": type}
    ).sort("createdAt", -1).to_list(50)
    return [_track_out(d) for d in docs]


# ── Add Spotify track ─────────────────────────────────────────────
class SpotifyBody(BaseModel):
    spotifyUrl: str
    caption:    Optional[str] = ""

@router.post("/music", status_code=201)
async def add_music(body: SpotifyBody, request: Request, user=Depends(get_current_user)):
    db = request.app.state.db

    # Build Spotify embed URL — accept track, album, or playlist
    url = body.spotifyUrl.strip()
    content_type = None
    content_id   = None

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
        raise HTTPException(status_code=400, detail="Invalid Spotify URL — paste a track, album or playlist link")

    embed_url   = f"https://open.spotify.com/embed/{content_type}/{content_id}"
    spotify_url = f"https://open.spotify.com/{content_type}/{content_id}"

    # Fetch oEmbed for title/artist/thumbnail
    title = ""; artist = ""; thumbnail = ""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                "https://open.spotify.com/oembed",
                params={"url": spotify_url}
            )
            if resp.status_code == 200:
                data = resp.json()
                title     = data.get("title", "")
                thumbnail = data.get("thumbnail_url", "")
                # title format is usually "Song – Artist"
                if " \u2013 " in title:
                    parts  = title.split(" \u2013 ", 1)
                    title  = parts[0].strip()
                    artist = parts[1].strip()
    except Exception:
        pass

    doc = {
        "_id":        _id(),
        "type":       "music",
        "spotifyUrl": spotify_url,
        "embedUrl":   embed_url,
        "title":      title,
        "artist":     artist,
        "thumbnail":  thumbnail,
        "caption":    (body.caption or "").strip()[:300],
        "username":   user.get("username", ""),
        "likeCount":  0,
        "commentCount": 0,
        "createdAt":  datetime.utcnow(),
    }
    await db.content.insert_one(doc)
    return _track_out(doc)


# ── Upload video ──────────────────────────────────────────────────
@router.post("/video", status_code=201)
async def upload_video(
    request: Request,
    caption: str = "",
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    if not file.content_type or not file.content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="File must be a video")

    file_bytes = await file.read()
    if len(file_bytes) > 100 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Video too large — max 100MB")

    if not CLOUD_NAME:
        raise HTTPException(status_code=500, detail="Video storage not configured")

    timestamp  = int(_time.time())
    public_id  = "beatfinder/videos/" + str(user.get("username", "")) + "_" + str(timestamp)
    to_sign    = f"public_id={public_id}&timestamp={timestamp}" + API_SECRET
    signature  = hashlib.sha256(to_sign.encode()).hexdigest()

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"https://api.cloudinary.com/v1_1/{CLOUD_NAME}/video/upload",
            data={
                "api_key":   API_KEY,
                "timestamp": timestamp,
                "public_id": public_id,
                "signature": signature,
            },
            files={"file": (file.filename, file_bytes, file.content_type)},
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Video upload failed")

    video_url  = resp.json().get("secure_url", "")
    thumbnail  = resp.json().get("secure_url", "").replace("/upload/", "/upload/so_0,w_400/").replace(".mp4", ".jpg")

    db = request.app.state.db
    doc = {
        "_id":        _id(),
        "type":       "video",
        "videoUrl":   video_url,
        "thumbnail":  thumbnail,
        "caption":    caption.strip()[:300],
        "username":   user.get("username", ""),
        "likeCount":  0,
        "commentCount": 0,
        "createdAt":  datetime.utcnow(),
    }
    await db.content.insert_one(doc)
    return _track_out(doc)


# ── Delete content ────────────────────────────────────────────────
@router.delete("/{content_id}")
async def delete_content(content_id: str, request: Request, user=Depends(get_current_user)):
    db     = request.app.state.db
    result = await db.content.delete_one(
        {"_id": content_id, "username": user.get("username")}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"deleted": True}


# ── Like / unlike content ─────────────────────────────────────────
@router.post("/{content_id}/like")
async def like_content(content_id: str, request: Request, user=Depends(get_current_user)):
    db       = request.app.state.db
    username = user.get("username")
    existing = await db.content_likes.find_one({"contentId": content_id, "username": username})
    if existing:
        await db.content_likes.delete_one({"_id": existing["_id"]})
        await db.content.update_one({"_id": content_id}, {"$inc": {"likeCount": -1}})
        return {"liked": False}
    await db.content_likes.insert_one({"contentId": content_id, "username": username, "createdAt": datetime.utcnow()})
    await db.content.update_one({"_id": content_id}, {"$inc": {"likeCount": 1}})
    return {"liked": True}


@router.get("/{content_id}/liked")
async def check_liked(content_id: str, request: Request, user=Depends(get_current_user)):
    db  = request.app.state.db
    hit = await db.content_likes.find_one({"contentId": content_id, "username": user.get("username")})
    return {"liked": bool(hit)}


# ── Comments ──────────────────────────────────────────────────────
class CommentBody(BaseModel):
    text:     str
    parentId: Optional[str] = None

@router.get("/{content_id}/comments")
async def get_comments(content_id: str, request: Request):
    db   = request.app.state.db
    docs = await db.content_comments.find(
        {"contentId": content_id}
    ).sort("createdAt", 1).to_list(200)
    return [_comment_out(d) for d in docs]


@router.post("/{content_id}/comments", status_code=201)
async def add_comment(
    content_id: str,
    body: CommentBody,
    request: Request,
    user=Depends(get_current_user),
):
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="Comment cannot be empty")

    db  = request.app.state.db
    doc = {
        "_id":       _id(),
        "contentId": content_id,
        "parentId":  body.parentId,
        "username":  user.get("username", ""),
        "avatarUrl": user.get("avatarUrl", ""),
        "text":      body.text.strip()[:500],
        "likeCount": 0,
        "createdAt": datetime.utcnow(),
    }
    await db.content_comments.insert_one(doc)
    await db.content.update_one({"_id": content_id}, {"$inc": {"commentCount": 1}})
    return _comment_out(doc)


@router.delete("/{content_id}/comments/{comment_id}")
async def delete_comment(
    content_id: str,
    comment_id: str,
    request: Request,
    user=Depends(get_current_user),
):
    db     = request.app.state.db
    result = await db.content_comments.delete_one(
        {"_id": comment_id, "username": user.get("username")}
    )
    if result.deleted_count:
        await db.content.update_one({"_id": content_id}, {"$inc": {"commentCount": -1}})
    return {"deleted": True}
