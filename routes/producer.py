from fastapi import APIRouter, Request, Depends, HTTPException, UploadFile, File, Form
from datetime import datetime
import httpx
import hashlib
import time
import os

from auth import get_current_user

router = APIRouter()

CLOUD_NAME  = os.getenv("CLOUDINARY_CLOUD_NAME", "")
API_KEY     = os.getenv("CLOUDINARY_API_KEY", "")
API_SECRET  = os.getenv("CLOUDINARY_API_SECRET", "")
UPLOAD_URL  = "https://api.cloudinary.com/v1_1/" + CLOUD_NAME + "/raw/upload"

STRIPE_SECRET  = os.getenv("STRIPE_SECRET_KEY", "")
FRONTEND_URL   = os.getenv("FRONTEND_URL", "https://beat-finder-frontend.vercel.app")
PLATFORM_FEE   = 1  # 1% platform fee

STRIPE_API     = "https://api.stripe.com/v1"
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")


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


# ── Upload a beat (Producer Pro only) ─────────────────────────────────────────

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
    user_doc = await db.users.find_one({"_id": user["_id"]})
    stripe_account_id = user_doc.get("stripe_account_id") if user_doc else None

    beat = {
        "title":             title,
        "genre":             genre,
        "price":             price,
        "url":               url,
        "producer":          user.get("name", "Unknown"),
        "producer_id":       str(user["_id"]),
        "producer_username": user.get("username", ""),
        "producer_avatar":   user_doc.get("avatarUrl", "") if user_doc else "",
        "stripe_account_id": stripe_account_id,
        "uploaded_at":       datetime.utcnow(),
        "downloads":         0,
        "playCount":         0,
    }
    result = await db.producer_beats.insert_one(beat)
    beat["_id"] = str(result.inserted_id)

    return {"success": True, "beat": beat}


# ── List all producer beats (public) ──────────────────────────────────────────

@router.get("/beats")
async def list_producer_beats(request: Request):
    db   = request.app.state.db
    docs = await db.producer_beats.find({}).sort("uploaded_at", -1).to_list(100)

    # Batch-fetch producer avatars
    producer_ids = list({d.get("producer_id") for d in docs if d.get("producer_id")})
    avatar_map = {}
    username_map = {}
    if producer_ids:
        from bson import ObjectId as _ObjId
        valid_ids = []
        for pid in producer_ids:
            try: valid_ids.append(_ObjId(pid))
            except Exception: pass
        if valid_ids:
            users = await db.users.find({"_id": {"$in": valid_ids}}, {"avatarUrl": 1, "username": 1}).to_list(100)
            for u in users:
                uid = str(u["_id"])
                avatar_map[uid]   = u.get("avatarUrl", "")
                username_map[uid] = u.get("username", "")

    return [
        {
            "id":                str(d["_id"]),
            "title":             d.get("title"),
            "genre":             d.get("genre"),
            "price":             d.get("price", "free"),
            "url":               d.get("url"),
            "producer":          d.get("producer"),
            "producer_id":       d.get("producer_id"),
            "producer_username": username_map.get(d.get("producer_id", ""), d.get("producer_username", "")),
            "producer_avatar":   avatar_map.get(d.get("producer_id", ""), d.get("producer_avatar", "")),
            "stripe_account_id": d.get("stripe_account_id"),
            "downloads":         d.get("downloads", 0),
            "playCount":         d.get("playCount", 0),
            "uploaded_at":       d.get("uploaded_at", "").isoformat() if d.get("uploaded_at") else "",
        }
        for d in docs
    ]


# ── My uploaded beats (producer only) ─────────────────────────────────────────

@router.get("/my-beats")
async def my_beats(request: Request, user=Depends(get_current_user)):
    db   = request.app.state.db
    docs = await db.producer_beats.find({"producer_id": str(user["_id"])}).sort("uploaded_at", -1).to_list(100)
    return [
        {
            "id":          str(d["_id"]),
            "title":       d.get("title"),
            "genre":       d.get("genre"),
            "price":       d.get("price", "free"),
            "downloads":   d.get("downloads", 0),
            "uploaded_at": d.get("uploaded_at", "").isoformat() if d.get("uploaded_at") else "",
        }
        for d in docs
    ]


# ── Connect Stripe account (Producer Pro) ─────────────────────────────────────

@router.post("/connect-stripe")
async def connect_stripe(request: Request, user=Depends(get_current_user)):
    if user.get("plan") != "producer":
        raise HTTPException(status_code=403, detail="Producer Pro required")

    # Get or create the Stripe account first
    account_id = await _get_or_create_stripe_account(user, request)

    # Auto-sync stripe_account_id to ALL existing beats by this producer
    db = request.app.state.db
    await db.producer_beats.update_many(
        {"producer_id": str(user["_id"])},
        {"$set": {"stripe_account_id": account_id}}
    )

    # Then create the account link
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            STRIPE_API + "/account_links",
            auth=(STRIPE_SECRET, ""),
            data={
                "account":     account_id,
                "refresh_url": FRONTEND_URL + "?stripe=refresh",
                "return_url":  FRONTEND_URL + "?stripe=connected",
                "type":        "account_onboarding",
            },
        )

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Stripe Connect error: " + r.text)

    return {"url": r.json()["url"]}


async def _get_or_create_stripe_account(user, request):
    db       = request.app.state.db
    user_doc = await db.users.find_one({"_id": user["_id"]})
    existing = user_doc.get("stripe_account_id") if user_doc else None

    if existing:
        return existing

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            STRIPE_API + "/accounts",
            auth=(STRIPE_SECRET, ""),
            data={
                "type":  "express",
                "email": user["email"],
                "capabilities[card_payments][requested]": "true",
                "capabilities[transfers][requested]":     "true",
                "business_type": "individual",
            },
        )

    if r.status_code != 200:
        err_msg = "Could not create Stripe account"
        try:
            err_msg = r.json().get("error", {}).get("message", err_msg)
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=err_msg)

    account_id = r.json()["id"]
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"stripe_account_id": account_id}}
    )
    return account_id


# ── Get Stripe connect status ──────────────────────────────────────────────────

@router.get("/stripe-status")
async def stripe_status(request: Request, user=Depends(get_current_user)):
    db       = request.app.state.db
    user_doc = await db.users.find_one({"_id": user["_id"]})
    account_id = user_doc.get("stripe_account_id") if user_doc else None

    if not account_id:
        return {"connected": False}

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            STRIPE_API + "/accounts/" + account_id,
            auth=(STRIPE_SECRET, ""),
        )

    if r.status_code != 200:
        return {"connected": False}

    data = r.json()
    return {
        "connected":   data.get("charges_enabled", False),
        "account_id":  account_id,
        "payouts_enabled": data.get("payouts_enabled", False),
    }


# ── Create lease checkout session ──────────────────────────────────────────────

@router.post("/beats/{beat_id}/buy-lease")
async def buy_lease(beat_id: str, request: Request, user=Depends(get_current_user)):
    from bson import ObjectId
    db   = request.app.state.db
    beat = await db.producer_beats.find_one({"_id": ObjectId(beat_id)})

    if not beat:
        raise HTTPException(status_code=404, detail="Beat not found")

    price_str = beat.get("price", "free")
    if price_str == "free":
        raise HTTPException(status_code=400, detail="This beat is free - no purchase needed")

    # Parse price (e.g. "£50" or "50")
    price_clean = price_str.replace("£", "").replace("$", "").strip()
    try:
        price_gbp = float(price_clean)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid price format")

    # Always look up the producer's current Stripe account from users collection
    producer_account = beat.get("stripe_account_id")
    if not producer_account:
        producer_doc = await db.users.find_one({"_id": __import__("bson").ObjectId(beat.get("producer_id", ""))})
        producer_account = producer_doc.get("stripe_account_id") if producer_doc else None

    if not producer_account:
        raise HTTPException(status_code=400, detail="Producer has not connected their Stripe account yet")

    # Also update the beat with the stripe account for future purchases
    await db.producer_beats.update_one(
        {"_id": ObjectId(beat_id)},
        {"$set": {"stripe_account_id": producer_account}}
    )

    price_pence       = int(price_gbp * 100)
    platform_fee_p    = max(1, int(price_pence * PLATFORM_FEE / 100))

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            STRIPE_API + "/checkout/sessions",
            auth=(STRIPE_SECRET, ""),
            data={
                "mode":                            "payment",
                "line_items[0][price_data][currency]":            "gbp",
                "line_items[0][price_data][product_data][name]":  beat.get("title", "Beat Lease"),
                "line_items[0][price_data][unit_amount]":         str(price_pence),
                "line_items[0][quantity]":                        "1",
                "customer_email":                                 user["email"],
                "payment_intent_data[application_fee_amount]":    str(platform_fee_p),
                "payment_intent_data[transfer_data][destination]": producer_account,
                "success_url":                                    FRONTEND_URL + "?lease=success&beat_id=" + beat_id,
                "cancel_url":                                     FRONTEND_URL + "?lease=cancelled",
                "metadata[beat_id]":                              beat_id,
                "metadata[buyer_id]":                             str(user["_id"]),
                "metadata[buyer_email]":                          user["email"],
                "metadata[producer_id]":                          beat.get("producer_id", ""),
                "metadata[type]":                                 "lease",
            },
        )

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Stripe error: " + r.text)

    return {"checkout_url": r.json()["url"]}


# ── Lease webhook - unlock beat for buyer after payment ────────────────────────

@router.post("/lease-webhook")
async def lease_webhook(request: Request):
    import hmac as hmac_mod
    import hashlib

    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    secret     = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    try:
        parts     = {p.split("=")[0]: p.split("=")[1] for p in sig_header.split(",")}
        timestamp = parts.get("t", "")
        signature = parts.get("v1", "")
        signed    = timestamp + "." + payload.decode("utf-8")
        expected  = hmac_mod.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
        if not hmac_mod.compare_digest(expected, signature):
            raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception:
        raise HTTPException(status_code=400, detail="Webhook verification failed")

    event = await request.json()

    if event.get("type") == "checkout.session.completed":
        session  = event["data"]["object"]
        metadata = session.get("metadata", {})

        if metadata.get("type") != "lease":
            return {"received": True}

        beat_id      = metadata.get("beat_id")
        buyer_id     = metadata.get("buyer_id")
        buyer_email  = metadata.get("buyer_email")

        if not all([beat_id, buyer_id]):
            return {"received": True}

        from bson import ObjectId
        db   = request.app.state.db
        beat = await db.producer_beats.find_one({"_id": ObjectId(beat_id)})
        if not beat:
            return {"received": True}

        # Add beat to buyer's purchased leases
        await db.purchased_leases.insert_one({
            "buyer_id":    buyer_id,
            "buyer_email": buyer_email,
            "beat_id":     beat_id,
            "beat_title":  beat.get("title"),
            "beat_url":    beat.get("url"),
            "producer":    beat.get("producer"),
            "price":       beat.get("price"),
            "purchased_at": datetime.utcnow(),
        })

        # Increment download count
        await db.producer_beats.update_one(
            {"_id": ObjectId(beat_id)},
            {"$inc": {"downloads": 1}}
        )

        print("[Lease] Beat " + beat_id + " purchased by " + buyer_email)

    return {"received": True}


# ── Get purchased leases for current user ─────────────────────────────────────

@router.get("/my-leases")
async def my_leases(request: Request, user=Depends(get_current_user)):
    db   = request.app.state.db
    docs = await db.purchased_leases.find({"buyer_id": str(user["_id"])}).sort("purchased_at", -1).to_list(100)
    return [
        {
            "id":           str(d["_id"]),
            "beat_title":   d.get("beat_title"),
            "beat_url":     d.get("beat_url"),
            "producer":     d.get("producer"),
            "price":        d.get("price"),
            "purchased_at": d.get("purchased_at", "").isoformat() if d.get("purchased_at") else "",
        }
        for d in docs
    ]


# ── Sync Stripe account to all producer beats ─────────────────────────────────

@router.post("/sync-stripe")
async def sync_stripe_to_beats(request: Request, user=Depends(get_current_user)):
    db       = request.app.state.db
    user_doc = await db.users.find_one({"_id": user["_id"]})
    account_id = user_doc.get("stripe_account_id") if user_doc else None

    if not account_id:
        raise HTTPException(status_code=400, detail="No Stripe account connected")

    result = await db.producer_beats.update_many(
        {"producer_id": str(user["_id"])},
        {"$set": {"stripe_account_id": account_id}}
    )
    return {"success": True, "updated": result.modified_count}


# ── Update beat details ───────────────────────────────────────────────────────

@router.post("/beats/{beat_id}/update")
async def update_beat(beat_id: str, request: Request, user=Depends(get_current_user)):
    from bson import ObjectId
    body = await request.json()
    db   = request.app.state.db

    update_fields = {}
    if body.get("title"):  update_fields["title"]  = body["title"].strip()
    if body.get("genre"):  update_fields["genre"]  = body["genre"].strip()
    if body.get("price"):  update_fields["price"]  = body["price"].strip()

    if not update_fields:
        raise HTTPException(status_code=400, detail="Nothing to update")

    result = await db.producer_beats.update_one(
        {"_id": ObjectId(beat_id), "producer_id": str(user["_id"])},
        {"$set": update_fields}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Beat not found or not yours")

    return {"success": True}


# ── Track download count ───────────────────────────────────────────────────────

@router.post("/beats/{beat_id}/download")
async def track_download(beat_id: str, request: Request):
    from bson import ObjectId
    db = request.app.state.db
    await db.producer_beats.update_one(
        {"_id": ObjectId(beat_id)},
        {"$inc": {"downloads": 1}}
    )
    return {"success": True}


# ── Proxy download — forces iOS Safari native download dialog ─────────────────
# iOS Safari shows "Do you want to download?" when:
#   - A user-gesture triggered anchor click hits a URL
#   - The server responds with Content-Disposition: attachment
# CORS headers allow cross-origin requests from Vercel frontend.

from fastapi.responses import StreamingResponse, Response
import re as _re

@router.options("/beats/{beat_id}/file")
async def proxy_download_options(beat_id: str):
    """Handle CORS preflight for the download route."""
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )

@router.get("/beats/{beat_id}/file")
async def proxy_download(beat_id: str, request: Request):
    from bson import ObjectId
    db   = request.app.state.db
    try:
        beat = await db.producer_beats.find_one({"_id": ObjectId(beat_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid beat ID")
    if not beat:
        raise HTTPException(status_code=404, detail="Beat not found")

    url = beat.get("url", "")
    if not url:
        raise HTTPException(status_code=404, detail="No file for this beat")

    # Safe filename for Content-Disposition
    raw_title  = beat.get("title", "beat")
    safe_title = _re.sub(r'[^\w\s\-]', '', raw_title).strip().replace(" ", "_") or "beat"
    filename   = safe_title + ".mp3"
    # RFC 5987 encoded filename for broad browser support
    encoded    = filename.encode("utf-8").decode("ascii", errors="replace")

    # Increment download count (fire-and-forget)
    await db.producer_beats.update_one(
        {"_id": ObjectId(beat_id)},
        {"$inc": {"downloads": 1}}
    )

    # HEAD the Cloudinary URL first to get Content-Length if available
    content_length = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            head = await client.head(url)
            cl   = head.headers.get("content-length")
            if cl:
                content_length = cl
    except Exception:
        pass

    async def generate():
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("GET", url) as resp:
                async for chunk in resp.aiter_bytes(65536):
                    yield chunk

    headers = {
        "Content-Disposition":    f'attachment; filename="{encoded}"',
        "Content-Type":           "audio/mpeg",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control":          "no-cache, no-store",
        "Access-Control-Allow-Origin": "*",
    }
    if content_length:
        headers["Content-Length"] = content_length

    return StreamingResponse(
        generate(),
        media_type="audio/mpeg",
        headers=headers,
    )


# ── Delete a beat ──────────────────────────────────────────────────────────────

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


# ── One-time backfill: sync producer_avatar onto all existing beats ────────────
# Call GET /api/producer/backfill-avatars?key=beatfinder_admin once after deploy

@router.get("/backfill-avatars")
async def backfill_avatars(request: Request, key: str = ""):
    if key != "beatfinder_admin":
        raise HTTPException(status_code=403, detail="Invalid key")
    from bson import ObjectId as _ObjId2
    db   = request.app.state.db
    docs = await db.producer_beats.find({}).to_list(1000)
    updated = 0
    errors  = []

    for d in docs:
        pid = d.get("producer_id")
        if not pid:
            errors.append({"beat": str(d.get("_id")), "error": "no producer_id"})
            continue
        u = None
        # Try ObjectId lookup first, then string lookup as fallback
        try:
            u = await db.users.find_one({"_id": _ObjId2(pid)}, {"avatarUrl": 1, "username": 1})
        except Exception:
            pass
        if not u:
            # producer_id might be stored as plain string username or email
            u = await db.users.find_one({"_id": pid}, {"avatarUrl": 1, "username": 1})
        if not u:
            errors.append({"beat": str(d.get("_id")), "producer_id": pid, "error": "user not found"})
            continue
        try:
            await db.producer_beats.update_one(
                {"_id": d["_id"]},
                {"$set": {
                    "producer_avatar":   u.get("avatarUrl", ""),
                    "producer_username": u.get("username", ""),
                    "playCount":         d.get("playCount", 0),
                }}
            )
            updated += 1
        except Exception as e:
            errors.append({"beat": str(d.get("_id")), "error": str(e)})

    return {"backfilled": updated, "total": len(docs), "errors": errors}
