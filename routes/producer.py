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
    title:       str        = Form(...),
    genre:       str        = Form(...),
    price:       str        = Form("free"),
    bpm:         str        = Form("0"),
    key:         str        = Form(""),
    description: str        = Form(""),
    preview_start: str      = Form("0"),
    # Two-tier lease pricing (only applies when price != "free"):
    #   - basic_lease_price: fixed at £50 — non-exclusive, 75% comp royalties to producer
    #   - premium_lease_price: producer-chosen £100-£500 — EXCLUSIVE, 50% comp royalties
    # When omitted the legacy single-price flow is used.
    basic_lease_price:   str = Form("50"),
    premium_lease_price: str = Form("0"),
    file:        UploadFile = File(...),
):
    if user.get("plan") != "producer":
        raise HTTPException(status_code=403, detail="Producer Pro plan required to upload beats")

    allowed_ext = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".aiff", ".opus")
    if not any(file.filename.lower().endswith(e) for e in allowed_ext):
        raise HTTPException(status_code=400, detail="Only MP3/WAV audio files are supported")

    file_bytes = await file.read()
    if len(file_bytes) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Maximum 50MB.")

    url = await upload_to_cloudinary(file_bytes, file.filename)

    db   = request.app.state.db
    user_doc = await db.users.find_one({"_id": user["_id"]})
    stripe_account_id = user_doc.get("stripe_account_id") if user_doc else None

    # Parse bpm safely
    try:
        bpm_val = int(bpm)
        if not (40 <= bpm_val <= 300):
            bpm_val = 0
    except Exception:
        bpm_val = 0

    # Parse preview_start safely
    try:
        ps_val = int(preview_start)
        if ps_val < 0: ps_val = 0
    except Exception:
        ps_val = 0

    # Parse two-tier lease prices. For paid beats (price != "free") we enforce:
    #   basic must be exactly 50 (fixed platform-wide standard)
    #   premium must be between 100 and 500 inclusive (producer choice)
    is_paid = (price or "free").strip().lower() not in ("free", "0", "£0", "£0.00", "")
    basic_price_val   = 50
    premium_price_val = 0
    if is_paid:
        try:
            basic_price_val = int(float(str(basic_lease_price).replace("£", "").strip()))
        except Exception:
            basic_price_val = 50
        if basic_price_val != 50:
            basic_price_val = 50  # silently normalise — basic is always £50

        try:
            premium_price_val = int(float(str(premium_lease_price).replace("£", "").strip()))
        except Exception:
            premium_price_val = 0
        if premium_price_val < 100 or premium_price_val > 500:
            raise HTTPException(status_code=400, detail="Premium lease price must be between £100 and £500")

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
        "description":       description.strip()[:500],
        "bpm":               bpm_val,
        "key":               key.strip()[:20],
        "preview_start":     ps_val,
        # Two-tier lease fields. For free beats these stay at 0/None.
        "basic_lease_price":   basic_price_val if is_paid else 0,
        "premium_lease_price": premium_price_val if is_paid else 0,
        "premium_sold":        False,  # flips True once a premium lease is paid
        "premium_sold_to":     None,   # buyer_id of the exclusive purchaser
        "premium_sold_at":     None,
    }
    result = await db.producer_beats.insert_one(beat)
    beat["_id"] = str(result.inserted_id)

    return {"success": True, "beat": beat}


# ── List all producer beats (public) ──────────────────────────────────────────

@router.get("/beats")
async def list_producer_beats(request: Request):
    db   = request.app.state.db
    # We expose premium_sold + premium_sold_to to the client so the frontend
    # can hide sold-exclusively beats from everyone except buyer and producer.
    # No server-side filtering needed — keeps endpoint public + simple.
    docs = await db.producer_beats.find({}).sort("uploaded_at", -1).to_list(200)

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
            "description":       d.get("description", ""),
            "bpm":               d.get("bpm", 0),
            "key":               d.get("key", ""),
            "preview_start":     d.get("preview_start", 0),
            "uploaded_at":       d.get("uploaded_at", "").isoformat() if d.get("uploaded_at") else "",
            # Two-tier fields. Defaults handle existing beats without these.
            "basic_lease_price":   d.get("basic_lease_price", 50 if d.get("price", "free") != "free" else 0),
            "premium_lease_price": d.get("premium_lease_price", 0),
            "premium_sold":        bool(d.get("premium_sold", False)),
            "premium_sold_to":     d.get("premium_sold_to"),
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
            "id":            str(d["_id"]),
            "title":         d.get("title"),
            "genre":         d.get("genre"),
            "price":         d.get("price", "free"),
            "downloads":     d.get("downloads", 0),
            "description":   d.get("description", ""),
            "bpm":           d.get("bpm", 0),
            "key":           d.get("key", ""),
            "preview_start": d.get("preview_start", 0),
            "uploaded_at":   d.get("uploaded_at", "").isoformat() if d.get("uploaded_at") else "",
            "basic_lease_price":   d.get("basic_lease_price", 50 if d.get("price", "free") != "free" else 0),
            "premium_lease_price": d.get("premium_lease_price", 0),
            "premium_sold":        bool(d.get("premium_sold", False)),
            "premium_sold_to":     d.get("premium_sold_to"),
            "premium_sold_at":     d.get("premium_sold_at", "").isoformat() if d.get("premium_sold_at") and hasattr(d.get("premium_sold_at"), "isoformat") else (d.get("premium_sold_at") or ""),
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
    """Initiate a Stripe checkout for either tier:
       - tier=basic   → £50 fixed,  non-exclusive, 75% royalties to producer
       - tier=premium → £100-£500, EXCLUSIVE, 50% royalties to producer
       Premium tier becomes unavailable once sold to a buyer."""
    from bson import ObjectId

    # Accept tier from JSON body OR query param. Default = "basic".
    tier = "basic"
    try:
        body = await request.json()
        if isinstance(body, dict) and body.get("tier"):
            tier = str(body["tier"]).strip().lower()
    except Exception:
        pass
    if not tier:
        tier = request.query_params.get("tier", "basic").strip().lower()
    if tier not in ("basic", "premium"):
        raise HTTPException(status_code=400, detail="tier must be 'basic' or 'premium'")

    db   = request.app.state.db
    beat = await db.producer_beats.find_one({"_id": ObjectId(beat_id)})

    if not beat:
        raise HTTPException(status_code=404, detail="Beat not found")

    price_str = beat.get("price", "free")
    if price_str == "free":
        raise HTTPException(status_code=400, detail="This beat is free - no purchase needed")

    # Block premium purchase if already sold
    if tier == "premium" and beat.get("premium_sold"):
        raise HTTPException(status_code=409, detail="The premium (exclusive) lease for this beat has already been sold")

    # Block basic purchases once premium has been sold — beat is fully retired.
    # Existing basic licences sold before the premium are voided automatically.
    if tier == "basic" and beat.get("premium_sold"):
        raise HTTPException(status_code=409, detail="This beat is no longer available — the exclusive (premium) lease has been sold")

    # Determine the correct price for the selected tier.
    # For legacy beats without explicit tier prices, fall back to the beat's
    # primary price for the basic tier and reject premium purchases.
    if tier == "basic":
        price_gbp = float(beat.get("basic_lease_price") or 0)
        if price_gbp <= 0:
            # Legacy beat — parse "price" field (e.g. "£50")
            try:
                price_gbp = float(str(price_str).replace("£", "").replace("$", "").strip())
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid price format")
        if price_gbp != 50.0:
            # Basic tier is always £50 platform-wide. Normalise.
            price_gbp = 50.0
    else:  # premium
        price_gbp = float(beat.get("premium_lease_price") or 0)
        if price_gbp < 100 or price_gbp > 500:
            raise HTTPException(status_code=400, detail="This beat does not offer a premium lease, or its premium price is invalid")

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
    product_name      = beat.get("title", "Beat Lease")
    if tier == "premium":
        product_name = product_name + " — Premium Exclusive Lease"
    else:
        product_name = product_name + " — Basic Lease"

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            STRIPE_API + "/checkout/sessions",
            auth=(STRIPE_SECRET, ""),
            data={
                "mode":                            "payment",
                "line_items[0][price_data][currency]":            "gbp",
                "line_items[0][price_data][product_data][name]":  product_name,
                "line_items[0][price_data][unit_amount]":         str(price_pence),
                "line_items[0][quantity]":                        "1",
                "customer_email":                                 user["email"],
                "payment_intent_data[application_fee_amount]":    str(platform_fee_p),
                "payment_intent_data[transfer_data][destination]": producer_account,
                "success_url":                                    FRONTEND_URL + "?lease=success&beat_id=" + beat_id,
                "cancel_url":                                     FRONTEND_URL + "?lease=cancelled",
                "metadata[beat_id]":                              beat_id,
                "metadata[beat_title]":                           beat.get("title", ""),
                "metadata[buyer_id]":                             str(user["_id"]),
                "metadata[buyer_email]":                          user["email"],
                "metadata[buyer_name]":                           user.get("name", user.get("username", "")),
                "metadata[buyer_username]":                       user.get("username", ""),
                "metadata[producer_id]":                          beat.get("producer_id", ""),
                "metadata[producer_name]":                        beat.get("producer", ""),
                "metadata[producer_username]":                    beat.get("producer_username", ""),
                "metadata[price]":                                "£" + str(int(price_gbp)),
                "metadata[type]":                                 "lease",
                "metadata[tier]":                                 tier,
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

        beat_id           = metadata.get("beat_id")
        buyer_id          = metadata.get("buyer_id")
        buyer_email       = metadata.get("buyer_email")
        buyer_name        = metadata.get("buyer_name", "")
        buyer_username    = metadata.get("buyer_username", "")
        producer_name     = metadata.get("producer_name", "")
        producer_username = metadata.get("producer_username", "")
        price             = metadata.get("price", "")
        tier              = (metadata.get("tier") or "basic").strip().lower()
        if tier not in ("basic", "premium"):
            tier = "basic"

        if not all([beat_id, buyer_id]):
            return {"received": True}

        from bson import ObjectId
        db   = request.app.state.db
        beat = await db.producer_beats.find_one({"_id": ObjectId(beat_id)})
        if not beat:
            return {"received": True}

        # Fallback producer info from beat doc if metadata missing
        if not producer_name:
            producer_name = beat.get("producer", "")
        if not producer_username:
            producer_username = beat.get("producer_username", "")
        if not price:
            price = beat.get("price", "")

        # Guard: refuse to double-sell a premium (exclusive) lease.
        # If a race happened and another buyer's webhook arrived first, refund
        # would be needed — but we still don't grant exclusive twice.
        if tier == "premium" and beat.get("premium_sold") and beat.get("premium_sold_to") != buyer_id:
            print(f"[Lease] WARNING: duplicate premium purchase attempt for beat {beat_id} by {buyer_email} — already sold to {beat.get('premium_sold_to')}")
            return {"received": True, "warning": "premium already sold"}

        # Add beat to buyer's purchased leases
        await db.purchased_leases.insert_one({
            "buyer_id":           buyer_id,
            "buyer_email":        buyer_email,
            "buyer_name":         buyer_name,
            "buyer_username":     buyer_username,
            "beat_id":            beat_id,
            "beat_title":         beat.get("title"),
            "beat_url":           beat.get("url"),
            "producer":           producer_name,
            "producer_username":  producer_username,
            "price":              price,
            "tier":               tier,
            "purchased_at":       datetime.utcnow(),
        })

        # Increment download count
        update_ops = {"$inc": {"downloads": 1}}

        # For premium tier — flip exclusivity flag so no one else can buy or see.
        if tier == "premium":
            update_ops.setdefault("$set", {})
            update_ops["$set"]["premium_sold"]    = True
            update_ops["$set"]["premium_sold_to"] = buyer_id
            update_ops["$set"]["premium_sold_at"] = datetime.utcnow()

        await db.producer_beats.update_one(
            {"_id": ObjectId(beat_id)},
            update_ops
        )

        # When premium is purchased, all previously-sold BASIC leases for this
        # beat become void. The basic buyers agreed at purchase time that the
        # licence is revocable on exclusive sale (clause 6 of the basic
        # contract) and acknowledged no refund.
        if tier == "premium":
            void_result = await db.purchased_leases.update_many(
                {
                    "beat_id": beat_id,
                    "tier":    "basic",
                    "voided":  {"$ne": True},
                },
                {"$set": {
                    "voided":        True,
                    "voided_at":     datetime.utcnow(),
                    "voided_reason": "premium_sold",
                }},
            )
            if void_result.modified_count > 0:
                print(f"[Lease] Premium sale voided {void_result.modified_count} prior basic lease(s) for beat {beat_id}")

        print(f"[Lease] Beat {beat_id} purchased ({tier}) by {buyer_email}")

    return {"received": True}


# ── Get purchased leases for current user ─────────────────────────────────────

@router.get("/my-leases")
async def my_leases(request: Request, user=Depends(get_current_user)):
    db   = request.app.state.db
    docs = await db.purchased_leases.find({"buyer_id": str(user["_id"])}).sort("purchased_at", -1).to_list(100)

    # Enrich each lease with buyer/producer data from users collection
    # This handles old leases that don't have these fields stored
    result = []
    for d in docs:
        # Get buyer info from current user (they are the buyer)
        buyer_name     = d.get("buyer_name") or user.get("name") or user.get("username", "")
        buyer_username = d.get("buyer_username") or user.get("username", "")
        buyer_email    = d.get("buyer_email") or user.get("email", "")

        # Get producer info — look up from beat if missing
        producer_name     = d.get("producer", "")
        producer_username = d.get("producer_username", "")
        if not producer_username and d.get("beat_id"):
            try:
                from bson import ObjectId as ObjId
                beat_doc = await db.producer_beats.find_one({"_id": ObjId(d["beat_id"])}, {"producer_username": 1, "producer": 1})
                if beat_doc:
                    producer_name     = producer_name or beat_doc.get("producer", "")
                    producer_username = beat_doc.get("producer_username", "")
            except Exception:
                pass

        result.append({
            "id":                str(d["_id"]),
            "beat_id":           d.get("beat_id", ""),
            "beat_title":        d.get("beat_title", ""),
            "beat_url":          d.get("beat_url", ""),
            "producer":          producer_name,
            "producer_username": producer_username,
            "buyer_name":        buyer_name,
            "buyer_username":    buyer_username,
            "buyer_email":       buyer_email,
            "price":             d.get("price", ""),
            "tier":              d.get("tier", "basic"),
            "voided":            bool(d.get("voided", False)),
            "voided_reason":     d.get("voided_reason", ""),
            "voided_at":         d.get("voided_at", "").isoformat() if d.get("voided_at") and hasattr(d.get("voided_at"), "isoformat") else (d.get("voided_at") or ""),
            "purchased_at":      d.get("purchased_at", "").isoformat() if d.get("purchased_at") else "",
        })
    return result


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

    # Look up the beat first so we know whether premium has been sold
    beat = await db.producer_beats.find_one({"_id": ObjectId(beat_id), "producer_id": str(user["_id"])})
    if not beat:
        raise HTTPException(status_code=404, detail="Beat not found or not yours")
    premium_sold = bool(beat.get("premium_sold"))

    update_fields = {}
    if body.get("title"):       update_fields["title"]       = body["title"].strip()
    if body.get("genre"):       update_fields["genre"]        = body["genre"].strip()
    if body.get("price"):       update_fields["price"]        = body["price"].strip()
    if "description" in body:   update_fields["description"]  = body["description"].strip()[:500]
    if "bpm" in body:
        try:
            bpm = int(body["bpm"])
            if 40 <= bpm <= 300: update_fields["bpm"] = bpm
        except: pass
    if "key" in body:           update_fields["key"]          = body["key"].strip()[:20]
    if "preview_start" in body:
        try:
            ps = int(body["preview_start"])
            if ps >= 0: update_fields["preview_start"] = ps
        except: pass

    # Two-tier lease price updates. Basic is locked to 50. Premium can be
    # changed by the producer between 100 and 500, but ONLY if not yet sold.
    if "basic_lease_price" in body:
        update_fields["basic_lease_price"] = 50  # always £50
    if "premium_lease_price" in body:
        if premium_sold:
            raise HTTPException(status_code=409, detail="Premium lease has already been sold — price is locked")
        try:
            pp = int(float(str(body["premium_lease_price"]).replace("£", "").strip()))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid premium lease price")
        if pp < 100 or pp > 500:
            raise HTTPException(status_code=400, detail="Premium lease price must be between £100 and £500")
        update_fields["premium_lease_price"] = pp

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
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, Range",
            "Access-Control-Expose-Headers": "Content-Length, Content-Disposition, Content-Type",
        }
    )


@router.head("/beats/{beat_id}/file")
async def proxy_download_head(beat_id: str, request: Request):
    """Lightweight HEAD so the client can probe availability before
    streaming. iOS Safari occasionally HEADs a media URL first."""
    from bson import ObjectId
    db = request.app.state.db
    try:
        beat = await db.producer_beats.find_one({"_id": ObjectId(beat_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid beat ID")
    if not beat or not beat.get("url"):
        raise HTTPException(status_code=404, detail="Not found")
    return Response(
        status_code=200,
        headers={
            "Content-Type": "audio/mpeg",
            "Access-Control-Allow-Origin": "*",
        },
    )


async def _try_get_user(request: Request):
    """Optional auth — returns the user dict if a valid token is in the
    Authorization header OR a `?token=…` query param, else None.
    Used by the MP3 download endpoint where iOS sometimes strips headers
    from media-download requests so the frontend includes a fallback token
    in the query string."""
    from auth import get_current_user as _gcu
    token = ""
    auth_hdr = request.headers.get("Authorization", "")
    if auth_hdr.lower().startswith("bearer "):
        token = auth_hdr.split(" ", 1)[1].strip()
    if not token:
        token = request.query_params.get("token", "").strip()
    if not token:
        return None
    # Build a synthetic request-like shim so we can reuse the existing
    # token-verification logic without re-implementing it. We add the token
    # back into Authorization header for the helper to read.
    try:
        # Rewrite the request's authorization header in-place for this call
        new_headers = [(k, v) for k, v in request.scope.get("headers", []) if k.lower() != b"authorization"]
        new_headers.append((b"authorization", ("Bearer " + token).encode()))
        request.scope["headers"] = new_headers
        return await _gcu(request)
    except Exception:
        return None


@router.get("/beats/{beat_id}/file")
async def proxy_download(beat_id: str, request: Request):
    """Stream the beat MP3 to the buyer.

    Access control:
      • FREE beats — any signed-in user can download. The frontend records
        contract acceptance before triggering this endpoint.
      • PAID (basic/premium) beats — the requester MUST own a non-voided
        lease in `purchased_leases`.
      • The beat's producer can always download their own beat.

    Stripe is the source of truth: a row in `purchased_leases` is only
    inserted by the `lease-webhook` once `checkout.session.completed`
    fires from Stripe. Cancelled or failed payments therefore cannot
    grant access here.
    """
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

    # ── ACCESS GATE ──────────────────────────────────────────────────────────
    requesting_user = await _try_get_user(request)

    price_str = (beat.get("price") or "free")
    is_paid_beat = price_str != "free"
    is_owner = bool(requesting_user and str(requesting_user.get("_id")) == beat.get("producer_id"))

    if is_paid_beat and not is_owner:
        if not requesting_user:
            raise HTTPException(status_code=401, detail="Sign in to download paid beats")
        # Look up the buyer's lease for this beat. Must exist and not be voided.
        lease = await db.purchased_leases.find_one({
            "beat_id":  beat_id,
            "buyer_id": str(requesting_user["_id"]),
        })
        if not lease:
            raise HTTPException(status_code=402, detail="Purchase required — no lease on file for this beat")
        if lease.get("voided"):
            raise HTTPException(status_code=410, detail="Your lease for this beat was revoked when the exclusive (premium) lease was sold to another buyer.")
    elif not is_paid_beat and not requesting_user:
        # Free beats still require sign-in so we can audit who downloaded.
        raise HTTPException(status_code=401, detail="Sign in to download")

    # Build a safe filename. Strict ASCII fallback PLUS the RFC 5987 filename*
    # form so all platforms (iOS, Android, desktop) get a sensible name even
    # when titles contain non-ASCII characters.
    raw_title  = beat.get("title", "beat")
    safe_ascii = _re.sub(r'[^A-Za-z0-9_\-]', '', raw_title.replace(" ", "_")) or "beat"
    safe_ascii = safe_ascii[:80] + ".mp3"
    from urllib.parse import quote as _urlquote
    utf8_name  = _urlquote((raw_title.strip() or "beat") + ".mp3", safe="")

    # Increment download count (fire-and-forget — must not block on error)
    try:
        await db.producer_beats.update_one(
            {"_id": ObjectId(beat_id)},
            {"$inc": {"downloads": 1}}
        )
    except Exception:
        pass

    async def generate():
        # One client per request keeps memory bounded on small Render instances
        timeout = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    return
                async for chunk in resp.aiter_bytes(65536):
                    yield chunk

    # IMPORTANT: do not set Content-Length here. Cloudinary may serve chunked
    # transfer encoding; if we set a Content-Length that doesn't match the
    # actual streamed body, iOS Safari shows a blank page and aborts.
    headers = {
        "Content-Disposition": (
            f"attachment; filename=\"{safe_ascii}\"; filename*=UTF-8''{utf8_name}"
        ),
        "Content-Type":            "audio/mpeg",
        "X-Content-Type-Options":  "nosniff",
        "Cache-Control":           "no-cache, no-store, must-revalidate",
        "Pragma":                  "no-cache",
        "Access-Control-Allow-Origin":   "*",
        "Access-Control-Expose-Headers": "Content-Length, Content-Disposition, Content-Type",
    }

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
