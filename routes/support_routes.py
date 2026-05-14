"""
support_routes.py — Contact / support form endpoint.

Routes:
  POST /api/contact   — submit a support enquiry; emails support@beatfinder.co.uk

Mounted in main.py as:
    from support_routes import router as support_router
    app.include_router(support_router, prefix="/api", tags=["Support"])

NOTE: Forgot-password and reset-password are owned by routes/auth.py.
This file used to duplicate them — that's been removed to avoid route conflicts
and a bug where the duplicate wrote to the wrong field (`password_hash` vs
`password`), silently breaking password resets.
"""
import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from routes.mailer import (
    send_email,
    contact_form_template,
    SUPPORT_EMAIL,
)

router = APIRouter()


# ── Contact form ─────────────────────────────────────────────────────────────
class ContactRequest(BaseModel):
    name:    str = Field(..., min_length=1, max_length=120)
    email:   EmailStr
    subject: str = Field(..., min_length=2, max_length=200)
    message: str = Field(..., min_length=10, max_length=5000)


# Simple in-memory rate limit: max 3 messages per email per 10 minutes.
# For a single-instance deploy this is fine. Swap to Redis if you scale out.
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

    if not _check_contact_rate(email):
        raise HTTPException(
            status_code=429,
            detail="Too many messages. Please wait a few minutes and try again.",
        )

    # Strip control chars — Pydantic already validates length
    def clean(s: str) -> str:
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s).strip()

    name    = clean(body.name)
    subject = clean(body.subject)
    message = clean(body.message)

    if not name or not subject or not message:
        raise HTTPException(status_code=400, detail="All fields are required.")

    html, text = contact_form_template(name, email, subject, message)
    ok = await send_email(
        to=SUPPORT_EMAIL,
        subject=f"[Support] {subject}",
        html=html,
        text=text,
        reply_to=email,
    )
    if not ok:
        raise HTTPException(
            status_code=502,
            detail="Couldn't send your message right now. Please email support@beatfinder.co.uk directly.",
        )

    # Log to Mongo for tracking — uses the shared app.state.db like every other route.
    # Wrapped because logging failure should never block a successfully-sent email.
    try:
        db = request.app.state.db
        await db.support_requests.insert_one({
            "name":       name,
            "email":      email,
            "subject":    subject,
            "message":    message,
            "created_at": datetime.now(timezone.utc),
            "ip":         request.client.host if request.client else None,
        })
    except Exception:
        pass

    return {"ok": True, "message": "Message sent. We'll reply by email as soon as we can."}
