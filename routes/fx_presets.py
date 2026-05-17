"""
FX Presets — admin-managed FX presets that all users can see.

Architecture:
  • Built-in presets (Vocal Clean, Polished, Hard Autotune, etc.) live as
    hardcoded constants in the frontend. They are NOT stored in MongoDB.
  • Admin-added presets ARE stored in MongoDB and fetched on Studio mount.
  • Hidden built-ins: admins can "hide" built-in presets. The list of
    hidden built-in IDs is stored in MongoDB and merged with the
    hardcoded list on the frontend.

Endpoints:
  GET    /api/fx-presets                       — public; returns admin-saved presets + hidden-builtin IDs
  POST   /api/admin/fx-presets                 — admin; save a new preset
  DELETE /api/admin/fx-presets/{id}            — admin; delete an admin-saved preset
  POST   /api/admin/fx-presets/hide-builtin    — admin; hide a built-in by id
  DELETE /api/admin/fx-presets/hide-builtin/{builtin_id} — admin; restore a hidden built-in

Storage model:
  MongoDB collection `fx_presets`:
    { _id, name, desc, fx, created_by, created_at, updated_at, kind: "user" }
  MongoDB collection `fx_presets_hidden`:
    { _id: "<builtin_id>", hidden_by, hidden_at }
"""
from fastapi import APIRouter, HTTPException, Request, Depends
from datetime import datetime
from typing import Any
import json

from auth import get_current_user, get_admin_user

router = APIRouter()

# Limits — keep reasonable since these are admin-curated, not user-spammed
PRESET_NAME_MAX = 80
PRESET_DESC_MAX = 200
PRESET_SIZE_MAX = 16 * 1024  # 16KB — FX state is small JSON
PRESET_COUNT_MAX = 100       # total admin-saved presets across all admins


def _validate_preset_body(body: Any) -> dict:
    """Sanity-check incoming preset shape. Doesn't deeply validate the fx
    object (frontend trusts it), but ensures name/desc are sane strings."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be JSON object")
    name = str(body.get("name", "")).strip()
    desc = str(body.get("desc", "")).strip()
    fx   = body.get("fx")
    if not name:
        raise HTTPException(status_code=400, detail="Preset name is required")
    if len(name) > PRESET_NAME_MAX:
        raise HTTPException(status_code=400, detail=f"Name too long (max {PRESET_NAME_MAX} chars)")
    if len(desc) > PRESET_DESC_MAX:
        raise HTTPException(status_code=400, detail=f"Description too long (max {PRESET_DESC_MAX} chars)")
    if not isinstance(fx, dict):
        raise HTTPException(status_code=400, detail="Preset fx must be an object")
    # Reject empty fx — no point saving a preset that does nothing
    if not any(v and isinstance(v, dict) and v.get("on") for v in fx.values()):
        raise HTTPException(status_code=400, detail="Preset must have at least one plugin enabled")
    # Size check — serialise and compare bytes
    encoded = json.dumps({"name": name, "desc": desc, "fx": fx})
    if len(encoded) > PRESET_SIZE_MAX:
        raise HTTPException(status_code=413, detail=f"Preset too large (max {PRESET_SIZE_MAX} bytes)")
    return {"name": name, "desc": desc, "fx": fx}


@router.get("/api/fx-presets")
async def list_presets(request: Request):
    """Public: anyone (logged in or not) can fetch admin-curated presets.

    Returns:
      {
        "presets": [...],            # admin-saved user presets
        "hidden_builtins": [...]     # IDs of built-ins admins have hidden
      }
    """
    db = request.app.state.db

    presets = []
    async for doc in db.fx_presets.find().sort("created_at", 1):
        presets.append({
            "id":         str(doc["_id"]),
            "name":       doc.get("name", "Untitled"),
            "desc":       doc.get("desc", ""),
            "fx":         doc.get("fx", {}),
            "kind":       "user",
            "created_by": doc.get("created_by_username", "admin"),
            "created_at": doc.get("created_at").isoformat() if isinstance(doc.get("created_at"), datetime) else None,
        })

    hidden = []
    async for doc in db.fx_presets_hidden.find():
        hidden.append(str(doc["_id"]))

    return {"presets": presets, "hidden_builtins": hidden}


@router.post("/api/admin/fx-presets")
async def create_preset(request: Request, user=Depends(get_admin_user)):
    """Admin: save a new preset. Body: { name, desc, fx }"""
    db = request.app.state.db
    body = await request.json()
    clean = _validate_preset_body(body)

    # Soft cap on total preset count to prevent runaway accumulation
    count = await db.fx_presets.count_documents({})
    if count >= PRESET_COUNT_MAX:
        raise HTTPException(status_code=400, detail=f"Preset limit reached ({PRESET_COUNT_MAX}). Delete some before adding more.")

    # Use a string id (timestamp + admin username) so frontend can reference
    # without needing ObjectId handling. Collisions virtually impossible at
    # millisecond resolution + per-admin namespace.
    import time as _time
    pid = f"u_{int(_time.time()*1000)}_{user.get('username','admin')}"

    now = datetime.utcnow()
    doc = {
        "_id":                  pid,
        "name":                 clean["name"],
        "desc":                 clean["desc"],
        "fx":                   clean["fx"],
        "created_by_user_id":   str(user["_id"]),
        "created_by_username":  user.get("username", "admin"),
        "created_at":           now,
        "updated_at":           now,
        "kind":                 "user",
    }
    await db.fx_presets.insert_one(doc)
    return {
        "id":         pid,
        "name":       clean["name"],
        "desc":       clean["desc"],
        "fx":         clean["fx"],
        "kind":       "user",
        "created_by": user.get("username", "admin"),
        "created_at": now.isoformat(),
    }


@router.delete("/api/admin/fx-presets/{preset_id}")
async def delete_preset(preset_id: str, request: Request, user=Depends(get_admin_user)):
    """Admin: delete an admin-saved preset by id."""
    db = request.app.state.db
    result = await db.fx_presets.delete_one({"_id": preset_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Preset not found")
    return {"deleted": preset_id}


@router.post("/api/admin/fx-presets/hide-builtin")
async def hide_builtin(request: Request, user=Depends(get_admin_user)):
    """Admin: hide a built-in preset by its hardcoded id.
    Body: { id: "clean" | "polished" | ... }"""
    db = request.app.state.db
    body = await request.json()
    builtin_id = str(body.get("id", "")).strip()
    if not builtin_id:
        raise HTTPException(status_code=400, detail="Built-in preset id is required")
    if len(builtin_id) > 100:
        raise HTTPException(status_code=400, detail="Id too long")
    # Upsert — multiple admins hiding the same built-in is fine
    await db.fx_presets_hidden.update_one(
        {"_id": builtin_id},
        {"$set": {
            "_id":        builtin_id,
            "hidden_by":  user.get("username", "admin"),
            "hidden_at":  datetime.utcnow(),
        }},
        upsert=True,
    )
    return {"hidden": builtin_id}


@router.delete("/api/admin/fx-presets/hide-builtin/{builtin_id}")
async def restore_builtin(builtin_id: str, request: Request, user=Depends(get_admin_user)):
    """Admin: restore a previously hidden built-in preset."""
    db = request.app.state.db
    await db.fx_presets_hidden.delete_one({"_id": builtin_id})
    return {"restored": builtin_id}
