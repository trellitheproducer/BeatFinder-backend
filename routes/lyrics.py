"""
Lyrics routes: /api/lyrics
All routes require authentication.
"""

from fastapi import APIRouter, Request, Depends, HTTPException
from datetime import datetime
from pydantic import BaseModel
from typing import List, Optional
from bson import ObjectId

from auth import get_current_user

router = APIRouter()


def _lyric_out(doc: dict) -> dict:
    return {
        "id":        str(doc.get("_id", doc.get("id", ""))),
        "title":     doc.get("title", "Untitled"),
        "text":      doc.get("text", ""),
        "beatTitle": doc.get("beatTitle", ""),
        "beatId":    doc.get("beatId", ""),
        "savedAt":   doc.get("savedAt", doc.get("created_at", datetime.utcnow())).isoformat()
                     if hasattr(doc.get("savedAt", doc.get("created_at")), "isoformat")
                     else str(doc.get("savedAt", "")),
        "updatedAt": doc.get("updatedAt", doc.get("updated_at", "")).isoformat()
                     if hasattr(doc.get("updatedAt", doc.get("updated_at", "")), "isoformat")
                     else str(doc.get("updatedAt", "")),
    }


# ── List all lyrics for current user ─────────────────────────────
@router.get("/")
async def list_lyrics(request: Request, user=Depends(get_current_user)):
    db   = request.app.state.db
    docs = await db.lyrics.find(
        {"user_id": str(user["_id"])}
    ).sort("created_at", -1).to_list(500)
    return [_lyric_out(d) for d in docs]


# ── Save / update a lyric ─────────────────────────────────────────
class LyricBody(BaseModel):
    id:        Optional[str] = None
    title:     Optional[str] = "Untitled"
    text:      str
    beatTitle: Optional[str] = ""
    beatId:    Optional[str] = ""
    savedAt:   Optional[str] = None

@router.post("/", status_code=201)
async def save_lyric(
    body: LyricBody,
    request: Request,
    user=Depends(get_current_user),
):
    db      = request.app.state.db
    user_id = str(user["_id"])
    now     = datetime.utcnow()

    # If an id is provided, try to update existing
    if body.id:
        result = await db.lyrics.update_one(
            {"_id": body.id, "user_id": user_id},
            {"$set": {
                "title":      body.title,
                "text":       body.text,
                "beatTitle":  body.beatTitle,
                "beatId":     body.beatId,
                "updated_at": now,
            }}
        )
        if result.matched_count > 0:
            return {"saved": True, "id": body.id}

    # Otherwise insert new
    lyric_id = body.id or str(ObjectId())
    doc = {
        "_id":        lyric_id,
        "user_id":    user_id,
        "title":      body.title or "Untitled",
        "text":       body.text,
        "beatTitle":  body.beatTitle or "",
        "beatId":     body.beatId or "",
        "created_at": now,
        "updated_at": now,
        "savedAt":    now,
    }
    await db.lyrics.insert_one(doc)
    return {"saved": True, "id": lyric_id}


# ── Delete a lyric ────────────────────────────────────────────────
@router.delete("/{lyric_id}")
async def delete_lyric(
    lyric_id: str,
    request: Request,
    user=Depends(get_current_user),
):
    db     = request.app.state.db
    result = await db.lyrics.delete_one(
        {"_id": lyric_id, "user_id": str(user["_id"])}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Lyric not found")
    return {"deleted": True, "id": lyric_id}


# ── Bulk import (migrate guest localStorage lyrics on login) ──────
class BulkImportBody(BaseModel):
    lyrics: List[LyricBody]

@router.post("/bulk-import", status_code=201)
async def bulk_import(
    body: BulkImportBody,
    request: Request,
    user=Depends(get_current_user),
):
    db      = request.app.state.db
    user_id = str(user["_id"])
    now     = datetime.utcnow()
    imported = 0

    for lyric in body.lyrics:
        # ── FIX: skip lyrics with no valid id — prevents DuplicateKeyError
        # on the user_id_1_lyric_id_1 index where lyric_id would be null
        if not lyric.id or not lyric.id.strip():
            continue

        lyric_id = lyric.id

        # Upsert — skip if already exists with same id
        result = await db.lyrics.update_one(
            {"_id": lyric_id, "user_id": user_id},
            {"$setOnInsert": {
                "_id":        lyric_id,
                "user_id":    user_id,
                "title":      lyric.title or "Untitled",
                "text":       lyric.text,
                "beatTitle":  lyric.beatTitle or "",
                "beatId":     lyric.beatId or "",
                "created_at": now,
                "updated_at": now,
                "savedAt":    now,
            }},
            upsert=True,
        )
        if result.upserted_id:
            imported += 1

    return {"imported": imported, "total": len(body.lyrics)}
