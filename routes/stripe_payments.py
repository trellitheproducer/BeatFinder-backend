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

# ── Monthly price IDs (existing, unchanged) ───────────────────────
ARTIST_PRICE_ID   = "price_1TQDoFFHyNSCxas89UpDKiro"
PRODUCER_PRICE_ID = "price_1TQDpBFHyNSCxas8cktbqw1n"

# ── Annual price IDs — create in Stripe Dashboard then add as env vars ──
# Artist Pro Annual:   £49.99/yr
# Producer Pro Annual: £89.99/yr
ARTIST_ANNUAL_PRICE_ID   = os.getenv("STRIPE_ARTIST_ANNUAL_PRICE_ID",   "price_ARTIST_ANNUAL_PLACEHOLDER")
PRODUCER_ANNUAL_PRICE_ID = os.getenv("STRIPE_PRODUCER_ANNUAL_PRICE_ID", "price_PRODUCER_ANNUAL_PLACEHOLDER")

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://beat-finder-frontend.vercel.app")


async def send_welcome_email(to_email: str, name: str, plan: str, billing: str = "monthly") -> bool:
    plan_label    = "Artist Pro" if plan == "artist" else "Producer Pro"
    billing_label = "Annual" if billing == "annual" else "Monthly"
    price_map = {
        "artist_monthly":   "£4.99/mo",
        "artist_annual":    "£49.99/yr",
        "producer_monthly": "£8.99/mo",
        "producer_annual":  "£89.99/yr",
    }
    price_label = price_map.get(plan + "_" + billing, "")
    html = """
<div style="font-family:sans-serif;max-width:500px;margin:0 auto;background:#0a0a0a;color:white;padding:32px;border-radius:16px">
  <div style="font-size:32px;font-weight:900;letter-spacing:4px;background:linear-gradient(135deg,#C026D3,#9333EA);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px">BEATFINDER</div>
  <div style="color:#888;margin-bottom:24px">The World's #1 Beat Finder App</div>
  <div style="color:white;font-size:20px;font-weight:700;margin-bottom:8px">Welcome to """ + plan_label + " (" + billing_label + """)!</div>
  <div style="color:#aaa;margin-bottom:24px">Hi """ + name + ", your " + price_label + """ subscription is now active. Your account has been automatically upgraded — just open the app and log in to access all your features.</div>
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
                    "subject": "Your BeatFinder " + plan_label + " (" + billing_label + ") is now active!",
                    "html":    html,
                },
            )
        return r.status_code == 200
    except Exception as e:
        print("[Email] Failed to send welcome email: " + str(e))
        return False


async def send_expiry_warning_email(to_email: str, name: str, plan: str, expires: str) -> bool:
    plan_label = "Artist Pro" if plan == "artist" else "Producer Pro"
    html = """
<div style="font-family:sans-serif;max-width:500px;margin:0 auto;background:#0a0a0a;color:white;padding:32px;border-radius:16px">
  <div style="font-size:32px;font-weight:900;letter-spacing:4px;color:#C026D3;margin-bottom:8px">BEATFINDER</div>
  <div style="color:white;font-size:18px;font-weight:700;margin-bottom:12px">Your """ + plan_label + """ subscription is expiring soon</div>
  <div style="color:#aaa;margin-bottom:24px">Hi """ + name + ", your subscription expires on <strong>" + expires + """</strong>. Renew now to keep access to all your pro features — you won't lose any of your content.</div>
  <div style="color:#555;font-size:12px;margin-top:24px">Questions? trellitheproducer@gmail.com</div>
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
                    "subject": "Your BeatFinder " + plan_label + " subscription expires soon",
                    "html":    html,
                },
            )
        return r.status_code == 200
    except Exception as e:
        print("[Email] Failed to send expiry warning: " + str(e))
        return False


# ── Create Stripe Checkout Session ───────────────────────────────────────────

@router.post("/create-checkout")
async def create_checkout(
    request: Request,
    user=Depends(get_current_user),
):
    body    = await request.json()

    # Accept price_id directly (preferred — sent by PlanPicker component)
    price_id = body.get("price_id")
    plan     = body.get("plan")
    billing  = body.get("billing", "monthly")

    # Map yearly placeholder IDs to real env-var IDs if needed
    YEARLY_MAP = {
        "price_artist_yearly_REPLACE":   ARTIST_ANNUAL_PRICE_ID,
        "price_producer_yearly_REPLACE": PRODUCER_ANNUAL_PRICE_ID,
    }
    if price_id and price_id in YEARLY_MAP:
        price_id = YEARLY_MAP[price_id]

    if not price_id:
        # Fall back to plan + billing lookup
        if plan not in ("artist", "producer"):
            raise HTTPException(status_code=400, detail="Invalid plan")
        if billing not in ("monthly", "annual", "yearly"):
            raise HTTPException(status_code=400, detail="Invalid billing interval")
        if plan == "artist":
            price_id = ARTIST_ANNUAL_PRICE_ID if billing in ("annual", "yearly") else ARTIST_PRICE_ID
        else:
            price_id = PRODUCER_ANNUAL_PRICE_ID if billing in ("annual", "yearly") else PRODUCER_PRICE_ID

    # Derive plan label for metadata if not provided
    if not plan:
        if price_id in (ARTIST_PRICE_ID, ARTIST_ANNUAL_PRICE_ID):
            plan = "artist"
        elif price_id in (PRODUCER_PRICE_ID, PRODUCER_ANNUAL_PRICE_ID):
            plan = "producer"
        else:
            plan = "unknown"

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.stripe.com/v1/checkout/sessions",
            auth=(STRIPE_SECRET, ""),
            data={
                "mode":                        "subscription",
                "line_items[0][price]":        price_id,
                "line_items[0][quantity]":     "1",
                "customer_email":              user["email"],
                "success_url":                 FRONTEND_URL + "?payment=success&plan=" + plan + "&billing=" + billing,
                "cancel_url":                  FRONTEND_URL + "?payment=cancelled",
                "metadata[user_id]":           str(user["_id"]),
                "metadata[user_email]":        user["email"],
                "metadata[user_name]":         user.get("name", ""),
                "metadata[plan]":              plan,
                "metadata[billing]":           billing,
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

        user_email    = metadata.get("user_email") or session.get("customer_email", "")
        user_name     = metadata.get("user_name", "")
        plan          = metadata.get("plan", "")
        billing       = metadata.get("billing", "monthly")
        period_end_ts = None
        sub_id        = session.get("subscription")

        # For invoice events, look up the subscription to get metadata + period_end
        if sub_id:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    sr = await client.get(
                        "https://api.stripe.com/v1/subscriptions/" + sub_id,
                        auth=(STRIPE_SECRET, ""),
                    )
                    sub_data      = sr.json()
                    period_end_ts = sub_data.get("current_period_end")
                    if not plan:
                        sub_meta = sub_data.get("metadata", {})
                        plan     = sub_meta.get("plan", "")
                        billing  = sub_meta.get("billing", "monthly")
                    if not user_email:
                        user_email = sub_data.get("customer_email", "")
                    if not user_name:
                        user_name  = sub_data.get("metadata", {}).get("user_name", "")
            except Exception as e:
                print("[Stripe] Could not fetch subscription: " + str(e))

        if not user_email or not plan:
            print("[Stripe] Missing email or plan in webhook metadata")
            return {"received": True}

        # Convert Unix timestamp → datetime
        expires_dt = datetime.utcfromtimestamp(period_end_ts) if period_end_ts else None

        # Upgrade the user in MongoDB
        plan_fields = {
            "plan":                    plan,
            "isPro":                   plan == "producer",
            "isArtistPro":             True,
            "upgraded_at":             datetime.utcnow(),
            "billing_interval":        billing,
            "stripe_subscription_id":  sub_id,
            "subscription_expires_at": expires_dt,   # ← new
        }

        result = await db.users.update_one(
            {"email": user_email},
            {"$set": plan_fields}
        )

        if result.modified_count > 0:
            print("[Stripe] Auto-upgraded " + user_email + " to " + plan + " (" + billing + "), expires " + str(expires_dt))
            sent = await send_welcome_email(user_email, user_name, plan, billing)
            print("[Stripe] Welcome email sent=" + str(sent))
        else:
            print("[Stripe] User not found for email: " + user_email)

    # ── Subscription renewed (next billing cycle paid) ────────────────────
    elif event.get("type") == "invoice.paid":
        # invoice.payment_succeeded above handles first payment;
        # invoice.paid fires on every renewal — update the expiry date
        session       = event["data"]["object"]
        sub_id        = session.get("subscription")
        period_end_ts = None
        user_email    = session.get("customer_email", "")

        if sub_id:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    sr = await client.get(
                        "https://api.stripe.com/v1/subscriptions/" + sub_id,
                        auth=(STRIPE_SECRET, ""),
                    )
                    sub_data      = sr.json()
                    period_end_ts = sub_data.get("current_period_end")
                    if not user_email:
                        user_email = sub_data.get("customer_email", "")
            except Exception as e:
                print("[Stripe] Could not fetch subscription for renewal: " + str(e))

        if user_email and period_end_ts:
            expires_dt = datetime.utcfromtimestamp(period_end_ts)
            await db.users.update_one(
                {"email": user_email},
                {"$set": {
                    "subscription_expires_at": expires_dt,
                    "stripe_subscription_id":  sub_id,
                }}
            )
            print("[Stripe] Renewed " + user_email + ", new expiry: " + str(expires_dt))

    # ── User cancelled — keeps access until period end ────────────────────
    elif event.get("type") == "customer.subscription.updated":
        session = event["data"]["object"]
        # Only act when the user has set cancel_at_period_end
        if session.get("cancel_at_period_end"):
            cancel_at   = session.get("cancel_at") or session.get("current_period_end")
            customer_id = session.get("customer")
            user_email  = ""
            user_doc    = None

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

            if user_email and cancel_at:
                expires_dt = datetime.utcfromtimestamp(cancel_at)
                await db.users.update_one(
                    {"email": user_email},
                    {"$set": {"subscription_expires_at": expires_dt}}
                )
                user_doc = await db.users.find_one({"email": user_email})
                plan     = user_doc.get("plan", "") if user_doc else ""
                exp_str  = expires_dt.strftime("%d %B %Y")
                await send_expiry_warning_email(
                    user_email,
                    user_doc.get("name", "") if user_doc else "",
                    plan,
                    exp_str,
                )
                print("[Stripe] " + user_email + " cancelled — access until " + str(expires_dt))

    # ── Subscription fully ended ──────────────────────────────────────────
    elif event.get("type") == "customer.subscription.deleted":
        session     = event["data"]["object"]
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
                    "plan":                    "free",
                    "isPro":                   False,
                    "isArtistPro":             False,
                    "downgraded_at":           datetime.utcnow(),
                    "subscription_expires_at": datetime.utcnow(),
                    "stripe_subscription_id":  None,
                }}
            )
            print("[Stripe] Downgraded " + user_email + " to free (subscription ended)")

    return {"received": True}
