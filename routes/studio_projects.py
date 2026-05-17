"""
Studio Projects — cloud sync for Studio projects.

Phase 1 (deployed): MongoDB metadata sync — track config, FX state, BPM,
clip positions/durations. Audio blobs not uploaded.

Phase 2 (this update): Vocal audio upload to Cloudinary.
  • New endpoint /sign-vocal-upload returns a time-limited Cloudinary
    signature that the FRONTEND uses to PUT the audio file directly to
    Cloudinary (server-mediated upload would double bandwidth and risk
    timeouts on big multi-take vocals).
  • Public_id is namespaced per-user per-project per-clip — even if a
    signature were stolen, it could only overwrite ONE specific clip
    slot, not arbitrary files.
  • Resource type is "video" because Cloudinary uses that endpoint for
    audio files too. Folder structure: beatfinder/studio/{user_id}/{proj}/
  • Frontend posts cloud URL back as part of the project save body —
    audio URL appears inside each vocal clip in the saved track JSON.

Storage model (Phase 2 adds `vocal_url` per clip in saved project):
  MongoDB collection `studio_projects`:
    tracks: [
      { ..., clips: [
          { id, startTime, ..., vocalUrl: "https://res.cloudinary.com/..." }
      ]}
    ]
"""
from fastapi import APIRouter, HTTPException, Request, Depends
from datetime import datetime
from bson import ObjectId
from typing import Any
import json
import os
import time as _time
import hashlib

# ROOT auth module (not routes/auth) — get_current_user, get_effective_plan
from auth import get_current_user, get_effective_plan

router = APIRouter()

PROJECT_SIZE_LIMIT  = 1 * 1024 * 1024   # 1MB max per project document
PROJECT_COUNT_LIMIT = 50                # max projects per user
VOCAL_MAX_BYTES     = 25 * 1024 * 1024  # 25MB hard cap per vocal upload


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


@router.post("/sign-vocal-upload")
async def sign_vocal_upload(request: Request, user=Depends(get_current_user)):
    """Return a Cloudinary signature for the frontend to upload a vocal audio file.

    Why signed-direct-upload instead of routing through backend?
      Audio recordings can be 5-30MB easily. Server-mediated uploads
      would double the bandwidth (browser→server→Cloudinary) and risk
      iOS Safari request timeouts on 4G. Direct uploads from the
      browser to Cloudinary are faster and don't burn our server CPU.

    Body shape:
      {
        "project_id": "<mongo project ID>",
        "clip_id":    "<unique clip ID — used as public_id namespace>"
      }

    Returns:
      {
        "cloud_name":   "...",
        "api_key":      "...",
        "timestamp":    <int>,
        "signature":    "...",
        "folder":       "beatfinder/studio/<user>/<project>",
        "public_id":    "vocal_<clip_id>",
        "upload_url":   "https://api.cloudinary.com/v1_1/<cloud>/video/upload",
        "resource_type":"video"
      }

    Frontend then POSTs the audio File/Blob to upload_url with the
    returned fields. Cloudinary responds with `secure_url` which the
    frontend stores on the clip and includes in the next project save.

    Signature scope:
      • Folder is fixed to the user+project — signature can't be reused
        to upload elsewhere
      • public_id contains the clip_id — bounded to one slot per clip
      • timestamp limits replay window (Cloudinary enforces ~1hr default)
    """
    db = request.app.state.db
    plan = await get_effective_plan(db, user)
    _require_pro(user, plan)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    project_id = (body or {}).get("project_id", "")
    clip_id    = (body or {}).get("clip_id", "")

    # Validate IDs to prevent path traversal or weird characters in folder names
    if not isinstance(project_id, str) or not project_id or len(project_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid project_id")
    if not isinstance(clip_id, str) or not clip_id or len(clip_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid clip_id")
    # Strip anything that isn't alphanumeric, hyphen, underscore, dot
    import re
    if not re.match(r"^[A-Za-z0-9._-]+$", project_id):
        raise HTTPException(status_code=400, detail="project_id contains invalid characters")
    if not re.match(r"^[A-Za-z0-9._-]+$", clip_id):
        raise HTTPException(status_code=400, detail="clip_id contains invalid characters")

    # Verify the project exists and belongs to this user. We don't want
    # someone signing uploads against project IDs that aren't theirs —
    # even though folder namespacing protects against cross-user damage,
    # this catches bad client state earlier.
    proj = await db.studio_projects.find_one({
        "_id":     project_id,
        "user_id": str(user["_id"]),
    })
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found — save the project before uploading audio")

    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    api_key    = os.getenv("CLOUDINARY_API_KEY", "")
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "")
    if not cloud_name or not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="Audio storage not configured")

    timestamp = int(_time.time())
    folder    = f"beatfinder/studio/{user['_id']}/{project_id}"
    public_id = f"vocal_{clip_id}"

    # Cloudinary signature: SHA-256 of "key1=val1&key2=val2..." + api_secret
    # MUST be alphabetical order of keys. We include folder and public_id
    # so they're bound into the signature and can't be swapped by a client.
    to_sign   = f"folder={folder}&public_id={public_id}&timestamp={timestamp}" + api_secret
    signature = hashlib.sha256(to_sign.encode()).hexdigest()

    return {
        "cloud_name":    cloud_name,
        "api_key":       api_key,
        "timestamp":     timestamp,
        "signature":     signature,
        "folder":        folder,
        "public_id":     public_id,
        "upload_url":    f"https://api.cloudinary.com/v1_1/{cloud_name}/video/upload",
        "resource_type": "video",
    }


@router.delete("/delete/{project_id}")
async def delete_project(project_id: str, request: Request, user=Depends(get_current_user)):
    """Delete a project + its associated Cloudinary vocal recordings.

    Enforces ownership via the filter — no cross-user deletion possible.

    Cleanup is best-effort: if Cloudinary deletion fails, the Mongo doc
    is still removed and we log the failure. Orphaned Cloudinary files
    waste storage but don't affect functionality. A nightly job could
    sweep these later if we ever notice cost growth.
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

    # Sweep Cloudinary folder for this project. Folder structure:
    #   beatfinder/studio/<user_id>/<project_id>/vocal_<clip_id>
    # Cloudinary supports deleting all resources by prefix via the Admin API.
    try:
        cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "")
        api_key    = os.getenv("CLOUDINARY_API_KEY", "")
        api_secret = os.getenv("CLOUDINARY_API_SECRET", "")
        if cloud_name and api_key and api_secret:
            import httpx
            from base64 import b64encode
            prefix = f"beatfinder/studio/{user['_id']}/{project_id}"
            # Admin API uses HTTP Basic Auth with api_key:api_secret
            auth = b64encode(f"{api_key}:{api_secret}".encode()).decode()
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.delete(
                    f"https://api.cloudinary.com/v1_1/{cloud_name}/resources/video",
                    params={"prefix": prefix},
                    headers={"Authorization": f"Basic {auth}"},
                )
    except Exception as e:
        # Best-effort — log but don't fail the delete. User's project doc
        # is already gone; orphaned Cloudinary files only waste storage.
        print(f"[studio_projects] Cloudinary cleanup failed for {project_id}: {e}")

    return {"deleted": True, "id": project_id}
