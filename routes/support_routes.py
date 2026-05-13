"""
support_routes.py — Endpoints for forgot/reset password and support contact form.

Routes:
  POST /api/auth/forgot-password   — request a password reset link
  POST /api/auth/reset-password    — set a new password using the reset token
  POST /api/contact                — submit a support enquiry form

Mount this router in main.py:
    from support_routes import router as support_router
    app.include_router(support_router, prefix="/api")

Dependencies expected:
  - mailer.send_email (and templates) from mailer.py
  - mongo collection `users`            with field `email`, `password_hash`
  - mongo collection `password_resets`  for one-time tokens (auto-cleaned)
  - bcrypt or passlib for password hashing — adjust to whatever you already use
  - `FRONTEND_URL` env var so the email link points back to your site

If your auth code uses a different password hashing setup, swap the
hash_password and the user lookup lines for whatever your existing code uses.
"""
import os
import secrets
import time
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel, EmailStr, Field

from mailer import (
    send_email,
    password_reset_template,
    contact_form_template,
    SUPPORT_EMAIL,
)

# ── Mongo handle ──────────────────────────────────────────────────────────────
# Adjust this import to wherever your existing code grabs the Mongo db object.
# In your producer.py for example you probably have something like:
#     from db import db
# Use the same import here so we're hitting the same database.
try:
    from db import db  # type: ignore
except ImportError:
    # Fallback — wire this to your actual Mongo connection
    from motor.motor_asyncio import AsyncIOMotorClient
    _client = AsyncIOMotorClient(os.getenv("MONGO_URL", "mongodb://localhost:27017"))
    db = _client[os.getenv("MONGO_DB", "beatfinder")]

# ── Password hashing ──────────────────────────────────────────────────────────
# Use whatever your existing auth uses. If you have passlib already imported
# elsewhere, just reuse that import. This bcrypt-direct path works as a default.
try:
    import bcrypt
    def hash_password(raw: str) -> str:
        return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
except ImportError:
    raise RuntimeError("bcrypt is required for password hashing — add it to requirements.txt")

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://beatfinder.co.uk").rstrip("/")

router = APIRouter()


# ════════════════════════════════════════════════════════════════════════════════
# FORGOT PASSWORD
# ════════════════════════════════════════════════════════════════════════════════
class ForgotPasswordRequest(BaseModel):
    email: EmailStr


@router.post("/auth/forgot-password")
async def forgot_password(body: ForgotPasswordRequest):
    """
    Generate a one-time reset token and email it. The endpoint ALWAYS responds
    success regardless of whether the email exists — this prevents attackers
    from probing which addresses are registered.
    """
    email = body.email.strip().lower()

    # Look up user — but don't reveal whether we found them
    user = await db.users.find_one({"email": email})

    if user:
        # Generate a cryptographically-secure one-time token
        token = secrets.token_urlsafe(48)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        # Store hashed token (so a DB leak can't be used to reset passwords)
        import hashlib
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

        await db.password_resets.insert_one({
            "user_id":    str(user["_id"]),
            "email":      email,
            "token_hash": token_hash,
            "expires_at": expires_at,
            "used":       False,
            "created_at": datetime.now(timezone.utc),
        })

        # Compose reset link (use a hash route or query param — match your frontend)
        reset_link = f"{FRONTEND_URL}/?reset_token={token}"

        # Send via Resend
        html, text = password_reset_template(
            reset_link=reset_link,
            user_name=user.get("name") or user.get("username") or "there",
        )
        await send_email(
            to=email,
            subject="Reset your BeatFinder password",
            html=html,
            text=text,
            reply_to=SUPPORT_EMAIL,
        )

    # Always respond success — never leak whether the email exists
    return {"ok": True, "message": "If an account exists for that email, a reset link has been sent."}


# ════════════════════════════════════════════════════════════════════════════════
# RESET PASSWORD
# ════════════════════════════════════════════════════════════════════════════════
class ResetPasswordRequest(BaseModel):
    token:        str = Field(..., min_length=10)
    new_password: str = Field(..., min_length=8, max_length=200)


@router.post("/auth/reset-password")
async def reset_password(body: ResetPasswordRequest):
    """
    Consume the one-time token and update the user's password hash.
    """
    import hashlib
    token_hash = hashlib.sha256(body.token.encode("utf-8")).hexdigest()

    # Find the unexpired, unused token
    now = datetime.now(timezone.utc)
    rec = await db.password_resets.find_one({
        "token_hash": token_hash,
        "used":       False,
        "expires_at": {"$gt": now},
    })
    if not rec:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired. Request a new one.")

    # Update the user's password
    new_hash = hash_password(body.new_password)
    upd = await db.users.update_one(
        {"email": rec["email"]},
        {"$set": {"password_hash": new_hash}},
    )
    if upd.matched_count == 0:
        raise HTTPException(status_code=400, detail="Account not found. Please contact support.")

    # Mark token used so it can't be replayed
    await db.password_resets.update_one(
        {"_id": rec["_id"]},
        {"$set": {"used": True, "used_at": now}},
    )

    return {"ok": True, "message": "Password updated. You can now log in with your new password."}


# ════════════════════════════════════════════════════════════════════════════════
# CONTACT / SUPPORT FORM
# ════════════════════════════════════════════════════════════════════════════════
class ContactRequest(BaseModel):
    name:    str = Field(..., min_length=1, max_length=120)
    email:   EmailStr
    subject: str = Field(..., min_length=2, max_length=200)
    message: str = Field(..., min_length=10, max_length=5000)


# Simple in-memory rate limit: max 3 messages per email per 10 minutes.
# (For production scale, swap to Redis. For your traffic this is fine.)
_contact_rate: dict[str, list[float]] = {}
_RATE_WINDOW_SEC = 600
_RATE_MAX        = 3


def _check_contact_rate(email: str) -> bool:
    now = time.time()
    hits = [t for t in _contact_rate.get(email, []) if now - t < _RATE_WINDOW_SEC]
    if len(hits) >= _RATE_MAX:
        return False
    hits.append(now)
    _contact_rate[email] = hits
    return True


@router.post("/contact")
async def submit_contact(body: ContactRequest, request: Request):
    """
    Accept a contact form submission and email it to support@beatfinder.co.uk.
    Reply-To is set to the user's email so support can reply directly.
    """
    email = body.email.strip().lower()

    # Rate limit
    if not _check_contact_rate(email):
        raise HTTPException(
            status_code=429,
            detail="Too many messages. Please wait a few minutes and try again.",
        )

    # Strip control chars from inputs (basic hygiene — Pydantic already validates length)
    def clean(s: str) -> str:
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s).strip()

    name    = clean(body.name)
    subject = clean(body.subject)
    message = clean(body.message)

    if not name or not subject or not message:
        raise HTTPException(status_code=400, detail="All fields are required.")

    # Build and send the email
    html, text = contact_form_template(name, email, subject, message)
    ok = await send_email(
        to=SUPPORT_EMAIL,
        subject=f"[Support] {subject}",
        html=html,
        text=text,
        reply_to=email,  # support's "Reply" goes straight to the user
    )
    if not ok:
        raise HTTPException(
            status_code=502,
            detail="Couldn't send your message right now. Please email support@beatfinder.co.uk directly.",
        )

    # Optional: log the request to Mongo for tracking
    try:
        await db.support_requests.insert_one({
            "name":       name,
            "email":      email,
            "subject":    subject,
            "message":    message,
            "created_at": datetime.now(timezone.utc),
            "ip":         request.client.host if request.client else None,
        })
    except Exception:
        pass  # logging failure shouldn't block the email succeeding

    return {"ok": True, "message": "Message sent. We'll reply by email as soon as we can."}
