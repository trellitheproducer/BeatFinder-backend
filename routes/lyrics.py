"""
Lyrics routes: /api/lyrics
All routes require authentication.
Lyrics are stored per user in MongoDB — never lost on app updates.
"""

from fastapi import APIRouter, Request, Depends, HTTPException
from datetime import datetime
from pydantic import BaseModel
from typing import Optional

from auth import get_current_user

router = APIRouter()


class LyricSave(BaseModel):
    id:         int
    title:      str
    text:       str
    beatTitle:  Optional[str] = ""
    beatId:     Optional[str] = ""
    savedAt:    Optional[str] = ""
    updatedAt:  Optional[str] = ""
    beat:       Optional[dict] = None  # full beat object {videoId, title, channel, thumbnail}


# ── List all lyrics for the current user ─────────────────────────
@router.get("/")
async def list_lyrics(request: Request, user=Depends(get_current_user)):
    db   = request.app.state.db
    docs = await db.lyrics.find(
        {"user_id": user["_id"]}
    ).sort("updated_at", -1).to_list(500)

    return [
        {
            "id":        d["lyric_id"],
            "title":     d.get("title", "Untitled"),
            "text":      d.get("text", ""),
            "beatTitle": d.get("beat_title", ""),
            "beatId":    d.get("beat_id", ""),
            "beat":      d.get("beat", None),
            "savedAt":   d.get("saved_at", ""),
            "updatedAt": d.get("updated_at", ""),
        }
        for d in docs
    ]


# ── Save or update a lyric ────────────────────────────────────────
@router.post("/", status_code=201)
async def save_lyric(
    body: LyricSave,
    request: Request,
    user=Depends(get_current_user),
):
    db  = request.app.state.db
    now = datetime.utcnow().isoformat()

    await db.lyrics.update_one(
        {"user_id": user["_id"], "lyric_id": body.id},
        {
            "$set": {
                "user_id":    user["_id"],
                "lyric_id":   body.id,
                "title":      body.title or "Untitled",
                "text":       body.text,
                "beat_title": body.beatTitle or "",
                "beat_id":    body.beatId or "",
                "beat":       body.beat,
                "saved_at":   body.savedAt or now,
                "updated_at": body.updatedAt or now,
            }
        },
        upsert=True,
    )
    return {"saved": True, "id": body.id}


# ── Delete a lyric ────────────────────────────────────────────────
@router.delete("/{lyric_id}")
async def delete_lyric(
    lyric_id: int,
    request: Request,
    user=Depends(get_current_user),
):
    db     = request.app.state.db
    result = await db.lyrics.delete_one(
        {"user_id": user["_id"], "lyric_id": lyric_id}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Lyric not found")
    return {"deleted": True, "id": lyric_id}


# ── Bulk import (migrate from localStorage on first login) ────────
@router.post("/bulk-import")
async def bulk_import(
    request: Request,
    user=Depends(get_current_user),
):
    body   = await request.json()
    lyrics = body.get("lyrics", [])
    if not lyrics:
        return {"imported": 0}

    db  = request.app.state.db
    now = datetime.utcnow().isoformat()
    count = 0

    for lyric in lyrics:
        lyric_id = lyric.get("id")
        if not lyric_id:
            continue
        # Only import if not already in DB
        existing = await db.lyrics.find_one(
            {"user_id": user["_id"], "lyric_id": lyric_id}
        )
        if existing:
            continue
        await db.lyrics.insert_one({
            "user_id":    user["_id"],
            "lyric_id":   lyric_id,
            "title":      lyric.get("title", "Untitled"),
            "text":       lyric.get("text", ""),
            "beat_title": lyric.get("beatTitle", ""),
            "beat_id":    lyric.get("beatId", ""),
            "beat":       lyric.get("beat", None),
            "saved_at":   lyric.get("savedAt", now),
            "updated_at": lyric.get("updatedAt", now),
        })
        count += 1

    return {"imported": count}
