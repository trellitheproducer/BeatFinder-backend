from fastapi import APIRouter, Request, HTTPException, Depends
from datetime import datetime
import httpx
import secrets
import os

from auth import get_current_user

router = APIRouter()

STRIPE_SECRET      = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SEC = os.getenv("STRIPE_WEBHOOK_SECRET", "")
RESEND_API_KEY     = os.getenv("RESEND_API_KEY", "")

ARTIST_PRICE_ID   = "price_1TQDoFFHyNSCxas89UpDKiro"
PRODUCER_PRICE_ID = "price_1TQDpBFHyNSCxas8cktbqw1n"

PRICE_TO_PLAN = {
    ARTIST_PRICE_ID:   "artist",
    PRODUCER_PRICE_ID: "producer",
}

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://beat-finder-frontend.vercel.app")


def gen_code(plan: str) -> str:
    prefix = "ART" if plan == "artist" else "PRD"
    return prefix + "-" + secrets.token_hex(3).upper()


async def send_activation_email(to_email: str, name: str, code: str, plan: str) -> bool:
    plan_label = "Artist Pro" if plan == "artist" else "Producer Pro"
    html = """
<div style="font-family:sans-serif;max-width:500px;margin:0 auto;background:#0a0a0a;color:white;padding:32px;border-radius:16px">
  <div style="font-size:32px;font-weight:900;letter-spacing:4px;background:linear-gradient(135deg,#C026D3,#9333EA);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px">BEATFINDER</div>
  <div style="color:#888;margin-bottom:24px">The World's #1 Beat Finder App</div>
  <div style="color:white;font-size:20px;font-weight:700;margin-bottom:8px">Your """ + plan_label + """ is ready!</div>
  <div style="color:#aaa;margin-bottom:24px">Hi """ + name + """, thank you for subscribing. Use the code below to activate your plan.</div>
  <div style="background:#1a1a1a;border:2px solid #C026D3;border-radius:12px;padding:24px;text-align:center;margin-bottom:24px">
    <div style="color:#888;font-size:13px;margin-bottom:8px">YOUR ACTIVATION CODE</div>
    <div style="color:#C026D3;font-size:28px;font-weight:900;letter-spacing:6px">""" + code + """</div>
  </div>
  <div style="color:#aaa;font-size:14px;margin-bottom:16px">To activate:</div>
  <ol style="color:#aaa;font-size:14px;line-height:2">
    <li>Open BeatFinder</li>
    <li>Go to Profile tab</li>
    <li>Select your plan and enter the code above</li>
    <li>Tap Activate with Code</li>
  </ol>
  <div style="color:#555;font-size:12px;margin-top:24px">This code is single-use only. If you need help contact us at trellitheproducer@gmail.com</div>
</div>
"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": "Bearer " + RESEND_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "from":    "BeatFinder <noreply@beatfinder.app>",
                "to":      [to_email],
                "subject": "Your BeatFinder " + plan_label + " Activation Code",
                "html":    html,
            },
        )
    return r.status_code == 200


# ── Create Stripe Checkout Session ───────────────────────────────────────────

@router.post("/create-checkout")
async def create_checkout(
    request: Request,
    user=Depends(get_current_user),
):
    body = await request.json()
    plan = body.get("plan")

    if plan not in ("artist", "producer"):
        raise HTTPException(status_code=400, detail="Invalid plan")

    price_id = ARTIST_PRICE_ID if plan == "artist" else PRODUCER_PRICE_ID

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.stripe.com/v1/checkout/sessions",
            auth=(STRIPE_SECRET, ""),
            data={
                "mode":                        "subscription",
                "line_items[0][price]":        price_id,
                "line_items[0][quantity]":     "1",
                "customer_email":              user["email"],
                "success_url":                 FRONTEND_URL + "?payment=success&plan=" + plan,
                "cancel_url":                  FRONTEND_URL + "?payment=cancelled",
                "metadata[user_id]":           str(user["_id"]),
                "metadata[user_email]":        user["email"],
                "metadata[user_name]":         user.get("name", ""),
                "metadata[plan]":              plan,
            },
        )

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Stripe error: " + r.text)

    session = r.json()
    return {"checkout_url": session["url"]}


# ── Stripe Webhook ────────────────────────────────────────────────────────────

@router.post("/webhook")
async def stripe_webhook(request: Request):
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # Verify webhook signature
    import hmac
    import hashlib

    try:
        parts     = {p.split("=")[0]: p.split("=")[1] for p in sig_header.split(",")}
        timestamp = parts.get("t", "")
        signature = parts.get("v1", "")
        signed    = timestamp + "." + payload.decode("utf-8")
        expected  = hmac.new(
            STRIPE_WEBHOOK_SEC.encode(),
            signed.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception:
        raise HTTPException(status_code=400, detail="Webhook verification failed")

    event = await request.json()

    if event.get("type") == "checkout.session.completed":
        session  = event["data"]["object"]
        metadata = session.get("metadata", {})

        user_id    = metadata.get("user_id")
        user_email = metadata.get("user_email")
        user_name  = metadata.get("user_name", "")
        plan       = metadata.get("plan")

        if not all([user_id, user_email, plan]):
            return {"received": True}

        db   = request.app.state.db
        code = gen_code(plan)

        # Store activation code in MongoDB
        await db.activation_codes.insert_one({
            "_id":        code,
            "plan":       plan,
            "used":       False,
            "user_email": user_email,
            "created_at": datetime.utcnow(),
        })

        # Send activation email
        sent = await send_activation_email(user_email, user_name, code, plan)

        print("[Stripe] Payment complete for " + user_email + " plan=" + plan + " code=" + code + " email_sent=" + str(sent))

    return {"received": True}
