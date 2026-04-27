from fastapi import APIRouter, Request, HTTPException, Depends
from datetime import datetime
import httpx
import hmac
import hashlib
import os

from auth import get_current_user

router = APIRouter()

STRIPE_SECRET      = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SEC = os.getenv("STRIPE_WEBHOOK_SECRET", "")
RESEND_API_KEY     = os.getenv("RESEND_API_KEY", "")

ARTIST_PRICE_ID   = "price_1TQDoFFHyNSCxas89UpDKiro"
PRODUCER_PRICE_ID = "price_1TQDpBFHyNSCxas8cktbqw1n"

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://beat-finder-frontend.vercel.app")


async def send_welcome_email(to_email: str, name: str, plan: str) -> bool:
    plan_label = "Artist Pro" if plan == "artist" else "Producer Pro"
    html = """
<div style="font-family:sans-serif;max-width:500px;margin:0 auto;background:#0a0a0a;color:white;padding:32px;border-radius:16px">
  <div style="font-size:32px;font-weight:900;letter-spacing:4px;background:linear-gradient(135deg,#C026D3,#9333EA);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px">BEATFINDER</div>
  <div style="color:#888;margin-bottom:24px">The World's #1 Beat Finder App</div>
  <div style="color:white;font-size:20px;font-weight:700;margin-bottom:8px">Welcome to """ + plan_label + """!</div>
  <div style="color:#aaa;margin-bottom:24px">Hi """ + name + """, your subscription is now active. Your account has been automatically upgraded — just open the app and log in to access all your features.</div>
  <div style="background:#1a1a1a;border:2px solid #C026D3;border-radius:12px;padding:24px;text-align:center;margin-bottom:24px">
    <div style="color:#C026D3;font-size:20px;font-weight:900;">✅ Account Upgraded</div>
    <div style="color:#888;font-size:14px;margin-top:8px">No code needed — you're all set!</div>
  </div>
  <div style="color:#aaa;font-size:14px;margin-bottom:16px">To get started:</div>
  <ol style="color:#aaa;font-size:14px;line-height:2">
    <li>Open BeatFinder</li>
    <li>Go to Profile tab</li>
    <li>Log in to your account</li>
    <li>Enjoy your """ + plan_label + """ features!</li>
  </ol>
  <div style="color:#555;font-size:12px;margin-top:24px">If you need help contact us at trellitheproducer@gmail.com</div>
</div>
"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": "Bearer " + RESEND_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "from":    "BeatFinder <onboarding@resend.dev>",
                    "to":      [to_email],
                    "subject": "Your BeatFinder " + plan_label + " is now active!",
                    "html":    html,
                },
            )
        return r.status_code == 200
    except Exception as e:
        print("[Email] Failed to send welcome email: " + str(e))
        return False


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
                "payment_method_types[0]":     "card",
                "custom_text[submit][message]":"Subscribe to BeatFinder",
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

    # Verify Stripe webhook signature — this is the security gate
    # Only real Stripe payments can pass this check
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
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Webhook verification failed")

    event = payload  # already bytes, parse json separately
    import json
    try:
        event = json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    db = request.app.state.db

    # ── Subscription created / payment succeeded ──────────────────────────
    if event.get("type") in ("checkout.session.completed", "invoice.payment_succeeded"):
        session  = event["data"]["object"]
        metadata = session.get("metadata", {})

        user_email = metadata.get("user_email") or session.get("customer_email", "")
        user_name  = metadata.get("user_name", "")
        plan       = metadata.get("plan", "")

        # For invoice events, look up the subscription to get metadata
        if not plan and event.get("type") == "invoice.payment_succeeded":
            sub_id = session.get("subscription")
            if sub_id:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        sr = await client.get(
                            "https://api.stripe.com/v1/subscriptions/" + sub_id,
                            auth=(STRIPE_SECRET, ""),
                        )
                        sub_data = sr.json()
                        metadata = sub_data.get("metadata", {})
                        plan       = metadata.get("plan", "")
                        user_email = metadata.get("user_email", user_email)
                        user_name  = metadata.get("user_name", user_name)
                except Exception as e:
                    print("[Stripe] Could not fetch subscription: " + str(e))

        if not user_email or not plan:
            print("[Stripe] Missing email or plan in webhook metadata")
            return {"received": True}

        # Upgrade the user directly in MongoDB — no code needed
        plan_fields = {
            "plan":         plan,
            "isPro":        plan == "producer",
            "isArtistPro":  True,
            "upgraded_at":  datetime.utcnow(),
        }

        result = await db.users.update_one(
            {"email": user_email},
            {"$set": plan_fields}
        )

        if result.modified_count > 0:
            print("[Stripe] Auto-upgraded " + user_email + " to " + plan)
            # Send welcome email (no code — just confirmation)
            sent = await send_welcome_email(user_email, user_name, plan)
            print("[Stripe] Welcome email sent=" + str(sent))
        else:
            print("[Stripe] User not found for email: " + user_email)

    # ── Subscription cancelled ────────────────────────────────────────────
    elif event.get("type") == "customer.subscription.deleted":
        session = event["data"]["object"]
        # Look up customer email from Stripe
        customer_id = session.get("customer")
        user_email  = ""
        if customer_id:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    cr = await client.get(
                        "https://api.stripe.com/v1/customers/" + customer_id,
                        auth=(STRIPE_SECRET, ""),
                    )
                    user_email = cr.json().get("email", "")
            except Exception as e:
                print("[Stripe] Could not fetch customer: " + str(e))

        if user_email:
            await db.users.update_one(
                {"email": user_email},
                {"$set": {
                    "plan":        "free",
                    "isPro":       False,
                    "isArtistPro": False,
                    "downgraded_at": datetime.utcnow(),
                }}
            )
            print("[Stripe] Downgraded " + user_email + " to free (subscription cancelled)")

    return {"received": True}
