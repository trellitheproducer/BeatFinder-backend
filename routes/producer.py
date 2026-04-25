from fastapi import APIRouter, Request, Depends, HTTPException, UploadFile, File, Form
from datetime import datetime
import httpx
import hashlib
import hmac
import time
import os

from auth import get_current_user

router = APIRouter()

CLOUD_NAME  = os.getenv("CLOUDINARY_CLOUD_NAME", "")
API_KEY     = os.getenv("CLOUDINARY_API_KEY", "")
API_SECRET  = os.getenv("CLOUDINARY_API_SECRET", "")
UPLOAD_URL  = "https://api.cloudinary.com/v1_1/" + CLOUD_NAME + "/raw/upload"


def cloudinary_signature(params: dict) -> str:
    sorted_params = "&".join(
        k + "=" + str(v)
        for k, v in sorted(params.items())
        if k not in ("api_key", "resource_type", "file")
    )
    to_sign = sorted_params + API_SECRET
    return hashlib.sha256(to_sign.encode()).hexdigest()


async def upload_to_cloudinary(file_bytes: bytes, filename: str) -> str:
    timestamp = int(time.time())
    folder    = "beatfinder/beats"
    params    = {"timestamp": timestamp, "folder": folder}
    signature = cloudinary_signature(params)

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            UPLOAD_URL,
            data={
                "api_key":   API_KEY,
                "timestamp": timestamp,
                "folder":    folder,
                "signature": signature,
            },
            files={"file": (filename, file_bytes, "audio/mpeg")},
        )

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Upload to Cloudinary failed: " + response.text)

    return response.json().get("secure_url", "")


# ── Upload a beat (Producer Pro only) ────────────────────────────────────────

@router.post("/upload", status_code=201)
async def upload_beat(
    request: Request,
    user=Depends(get_current_user),
    title:  str        = Form(...),
    genre:  str        = Form(...),
    price:  str        = Form("free"),
    file:   UploadFile = File(...),
):
    if user.get("plan") != "producer":
        raise HTTPException(status_code=403, detail="Producer Pro plan required to upload beats")

    if not file.filename.lower().endswith(".mp3"):
        raise HTTPException(status_code=400, detail="Only MP3 files are supported")

    file_bytes = await file.read()
    if len(file_bytes) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Maximum 50MB.")

    url = await upload_to_cloudinary(file_bytes, file.filename)

    db   = request.app.state.db
    beat = {
        "title":       title,
        "genre":       genre,
        "price":       price,
        "url":         url,
        "producer":    user.get("name", "Unknown"),
        "producer_id": str(user["_id"]),
        "uploaded_at": datetime.utcnow(),
        "downloads":   0,
    }
    result = await db.producer_beats.insert_one(beat)
    beat["_id"] = str(result.inserted_id)

    return {"success": True, "beat": beat}


# ── List all producer beats (public) ─────────────────────────────────────────

@router.get("/beats")
async def list_producer_beats(request: Request):
    db   = request.app.state.db
    docs = await db.producer_beats.find({}).sort("uploaded_at", -1).to_list(100)
    return [
        {
            "id":          str(d["_id"]),
            "title":       d.get("title"),
            "genre":       d.get("genre"),
            "price":       d.get("price", "free"),
            "url":         d.get("url"),
            "producer":    d.get("producer"),
            "downloads":   d.get("downloads", 0),
            "uploaded_at": d.get("uploaded_at", "").isoformat() if d.get("uploaded_at") else "",
        }
        for d in docs
    ]


# ── Track download count ──────────────────────────────────────────────────────

@router.post("/beats/{beat_id}/download")
async def track_download(beat_id: str, request: Request):
    from bson import ObjectId
    db = request.app.state.db
    await db.producer_beats.update_one(
        {"_id": ObjectId(beat_id)},
        {"$inc": {"downloads": 1}}
    )
    return {"success": True}


# ── Delete a beat (producer can delete their own) ────────────────────────────

@router.delete("/beats/{beat_id}")
async def delete_beat(beat_id: str, request: Request, user=Depends(get_current_user)):
    from bson import ObjectId
    db     = request.app.state.db
    result = await db.producer_beats.delete_one({
        "_id":         ObjectId(beat_id),
        "producer_id": str(user["_id"]),
    })
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Beat not found or not yours")
    return {"success": True}
