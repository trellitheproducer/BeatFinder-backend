"""
Studio Projects — cloud sync for Studio projects.

Phase 1 of the project sync rollout. Provides CRUD over a user's project
metadata (track config, FX state, BPM, clip positions/durations, clip
URLs). Audio buffers themselves are NOT uploaded here — that's Phase 2.
For now:
  • Beat clips reference an existing Cloudinary URL (already-uploaded)
  • Vocal clips are stored with no audio URL → audio doesn't survive
    reinstall yet. Will be added in Phase 2.

Storage model:
  MongoDB collection `studio_projects`:
    {
      _id: <str ObjectId>,
      user_id: <str>,
      name: <str>,
      bpm: <int>, key: <str>, time_sig_num: <int>,
      loop_in: <float>, loop_out: <float>,
      master_volume: <float>,
      tracks: [ ... track objects with clips ... ],
      created_at: <datetime>,
      updated_at: <datetime>,
      size_bytes: <int>,  // approximate JSON size for quota tracking
    }

Access control:
  • All endpoints require authenticated user
  • Cloud sync is a Pro feature — gated via get_effective_plan
  • Users can only access their own projects (user_id match enforced)

Limits:
  • PROJECT_SIZE_LIMIT: 1MB per project (metadata only)
  • PROJECT_COUNT_LIMIT: 50 projects per user (Pro/lifetime)
  • Free users blocked at the endpoint level entirely
"""
from fastapi import APIRouter, HTTPException, Request, Depends
from datetime import datetime
from bson import ObjectId
from typing import Any
import json

# ROOT auth module (not routes/auth) — get_current_user, get_effective_plan
from auth import get_current_user, get_effective_plan

router = APIRouter()

PROJECT_SIZE_LIMIT  = 1 * 1024 * 1024   # 1MB max per project document
PROJECT_COUNT_LIMIT = 50                # max projects per user


def _require_pro(user, effective_plan):
    """Gate: Studio project cloud sync is Pro/Premium only.

    Free users get a clear error pointing to upgrade. Lifetime accounts
    pass through normally via get_effective_plan returning their granted
    plan tier.
    """
    if effective_plan not in ("artist", "producer"):
        raise HTTPException(
            status_code=403,
            detail=(
                "Studio project cloud sync is a Pro feature. "
                "Upgrade to Artist Pro or Producer Pro to back up your projects."
            ),
        )


def _sanitize_project(p: dict, user_id: str) -> dict:
    """Normalize a project dict into the storage shape, enforcing types.

    Strips client-supplied fields we control (user_id, timestamps, _id)
    so clients can't spoof them. Defaults for missing fields prevent
    KeyError downstream.
    """
    if not isinstance(p, dict):
        raise HTTPException(status_code=400, detail="Project must be an object")
    name = (p.get("name") or "").strip()
    if not name:
        name = "Untitled Project"
    if len(name) > 120:
        name = name[:120]
    tracks = p.get("tracks") or []
    if not isinstance(tracks, list):
        tracks = []
    return {
        "name":          name,
        "bpm":           int(p.get("bpm") or 120),
        "key":           str(p.get("key") or "C major")[:32],
        "time_sig_num":  int(p.get("time_sig_num") or 4),
        "loop_in":       float(p.get("loop_in")  or 0),
        "loop_out":      float(p.get("loop_out") or 0),
        "master_volume": float(p.get("master_volume") or 1.0),
        "tracks":        tracks,  # passed through — frontend owns the schema
        "user_id":       user_id,
    }


@router.get("/list")
async def list_projects(request: Request, user=Depends(get_current_user)):
    """Return the user's project list (lightweight — no track data).

    For populating the "Open Project" picker. Each entry has just enough
    to display in a list. Full project data is fetched on open via /get.
    """
    db = request.app.state.db
    plan = await get_effective_plan(db, user)
    _require_pro(user, plan)

    cursor = db.studio_projects.find(
        {"user_id": str(user["_id"])},
        {
            "_id": 1, "name": 1, "bpm": 1, "key": 1,
            "created_at": 1, "updated_at": 1, "size_bytes": 1,
        },
    ).sort("updated_at", -1).limit(PROJECT_COUNT_LIMIT)

    projects = []
    async for doc in cursor:
        projects.append({
            "id":          doc["_id"],
            "name":        doc.get("name", "Untitled"),
            "bpm":         doc.get("bpm", 120),
            "key":         doc.get("key", "C major"),
            "created_at":  doc.get("created_at").isoformat() if isinstance(doc.get("created_at"), datetime) else None,
            "updated_at":  doc.get("updated_at").isoformat() if isinstance(doc.get("updated_at"), datetime) else None,
            "size_bytes":  doc.get("size_bytes", 0),
        })
    return {"projects": projects, "count": len(projects), "limit": PROJECT_COUNT_LIMIT}


@router.get("/get/{project_id}")
async def get_project(project_id: str, request: Request, user=Depends(get_current_user)):
    """Return the full project document for opening in Studio.

    Enforces ownership — even if a user somehow obtains another user's
    project ID, the query filter on user_id prevents access.
    """
    db = request.app.state.db
    plan = await get_effective_plan(db, user)
    _require_pro(user, plan)

    doc = await db.studio_projects.find_one({
        "_id":     project_id,
        "user_id": str(user["_id"]),
    })
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found")

    # Convert datetimes to ISO for JSON serialization
    doc["created_at"] = doc.get("created_at").isoformat() if isinstance(doc.get("created_at"), datetime) else None
    doc["updated_at"] = doc.get("updated_at").isoformat() if isinstance(doc.get("updated_at"), datetime) else None
    doc["id"] = doc.pop("_id")
    return doc


@router.post("/save")
async def save_project(request: Request, user=Depends(get_current_user)):
    """Create or upsert a project.

    Body shape:
      {
        "id":   "<optional — provide for update, omit for create>",
        "name": "...",
        "bpm": 120, "key": "C minor", "time_sig_num": 4,
        "loop_in": 0, "loop_out": 0, "master_volume": 1,
        "tracks": [...]
      }

    Returns:
      { "id": "...", "size_bytes": N, "saved_at": "..." }
    """
    db = request.app.state.db
    plan = await get_effective_plan(db, user)
    _require_pro(user, plan)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be an object")

    project_id = body.get("id")
    user_id    = str(user["_id"])
    now        = datetime.utcnow()

    sanitized = _sanitize_project(body, user_id)

    # Size check — reject oversized projects with a clear error.
    # Calculated on the sanitized doc so clients can't sneak extra fields
    # past the limit.
    size_bytes = len(json.dumps(sanitized, default=str).encode("utf-8"))
    if size_bytes > PROJECT_SIZE_LIMIT:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Project too large ({size_bytes // 1024}KB). "
                f"Max is {PROJECT_SIZE_LIMIT // 1024}KB. "
                "Consider removing unused tracks or splitting into smaller projects."
            ),
        )
    sanitized["size_bytes"] = size_bytes
    sanitized["updated_at"] = now

    if project_id:
        # Update existing — verify ownership in the filter, not blindly
        existing = await db.studio_projects.find_one({
            "_id":     project_id,
            "user_id": user_id,
        })
        if not existing:
            raise HTTPException(status_code=404, detail="Project not found")
        await db.studio_projects.update_one(
            {"_id": project_id, "user_id": user_id},
            {"$set": sanitized},
        )
        return {
            "id":         project_id,
            "size_bytes": size_bytes,
            "saved_at":   now.isoformat(),
            "created":    False,
        }

    # New project — enforce per-user count limit. Free tier returned 403
    # above, so this is the paid-user soft cap.
    count = await db.studio_projects.count_documents({"user_id": user_id})
    if count >= PROJECT_COUNT_LIMIT:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Project limit reached ({PROJECT_COUNT_LIMIT}). "
                "Delete some old projects to make room."
            ),
        )

    new_id = str(ObjectId())
    sanitized["_id"]        = new_id
    sanitized["created_at"] = now
    await db.studio_projects.insert_one(sanitized)
    return {
        "id":         new_id,
        "size_bytes": size_bytes,
        "saved_at":   now.isoformat(),
        "created":    True,
    }


@router.delete("/delete/{project_id}")
async def delete_project(project_id: str, request: Request, user=Depends(get_current_user)):
    """Delete a project.

    Enforces ownership via the filter — no cross-user deletion possible.
    """
    db = request.app.state.db
    plan = await get_effective_plan(db, user)
    _require_pro(user, plan)

    result = await db.studio_projects.delete_one({
        "_id":     project_id,
        "user_id": str(user["_id"]),
    })
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"deleted": True, "id": project_id}
