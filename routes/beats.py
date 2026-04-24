"""
Saved beats routes: /api/beats
All routes require authentication.
"""

from fastapi import APIRouter, Request, Depends, HTTPException
from datetime import datetime

from models import SaveBeatRequest
from auth import get_current_user

router = APIRouter()


# ── List saved beats ──────────────────────────────────────────────
@router.get("/")
async def list_saved(request: Request, user=Depends(get_current_user)):
    db = request.app.state.db
    docs = await db.saved_beats.find(
        {"user_id": user["_id"]}
    ).sort("saved_at", -1).to_list(200)

    return [
        {
            "video_id":  d["video_id"],
            "title":     d["title"],
            "channel":   d["channel"],
            "thumbnail": d["thumbnail"],
            "saved_at":  d["saved_at"].isoformat(),
        }
        for d in docs
    ]


# ── Save a beat ───────────────────────────────────────────────────
@router.post("/", status_code=201)
async def save_beat(
    body: SaveBeatRequest,
    request: Request,
    user=Depends(get_current_user),
):
    db = request.app.state.db
    beat = body.beat

    # Upsert — safe to call multiple times
    await db.saved_beats.update_one(
        {"user_id": user["_id"], "video_id": beat.video_id},
        {
            "$set": {
                "user_id":   user["_id"],
                "video_id":  beat.video_id,
                "title":     beat.title,
                "channel":   beat.channel,
                "thumbnail": beat.thumbnail,
                "saved_at":  datetime.utcnow(),
            }
        },
        upsert=True,
    )
    return {"saved": True, "video_id": beat.video_id}


# ── Remove a saved beat ───────────────────────────────────────────
@router.delete("/{video_id}")
async def remove_beat(
    video_id: str,
    request: Request,
    user=Depends(get_current_user),
):
    db     = request.app.state.db
    result = await db.saved_beats.delete_one(
        {"user_id": user["_id"], "video_id": video_id}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Beat not found in saved list")
    return {"removed": True, "video_id": video_id}
