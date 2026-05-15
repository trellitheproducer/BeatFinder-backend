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


async def send_payment_failed_email(
    to_email: str, name: str, plan: str, attempt_count: int, next_attempt_unix: int = 0
) -> bool:
    """Sent when Stripe fails to charge a subscription renewal. We tell the
    user what to do (update card via the Customer Portal) and how long they
    have. Stripe will automatically retry the charge a few times over the
    next ~3 weeks before giving up and cancelling the subscription."""
    plan_label = "Artist Pro" if plan == "artist" else "Producer Pro"

    next_attempt_str = ""
    if next_attempt_unix:
        try:
            next_attempt_dt  = datetime.utcfromtimestamp(next_attempt_unix)
            next_attempt_str = next_attempt_dt.strftime("%d %B %Y")
        except Exception:
            next_attempt_str = ""

    if attempt_count <= 1:
        headline_color = "#F59E0B"  # amber — first warning
        headline       = "We couldn't process your payment"
        urgency        = ""
    elif attempt_count <= 3:
        headline_color = "#F97316"  # orange — second warning
        headline       = "Payment still failing — action needed"
        urgency        = "<div style='color:#F97316;font-weight:700;margin-bottom:16px'>This is attempt " + str(attempt_count) + ". If we can't charge your card, you'll lose access to your pro features.</div>"
    else:
        headline_color = "#EF4444"  # red — last warning
        headline       = "Final notice — subscription about to be cancelled"
        urgency        = "<div style='color:#EF4444;font-weight:700;margin-bottom:16px'>This was our last attempt. Your subscription will be cancelled in a few days unless you update your payment method now.</div>"

    next_charge_html = ""
    if next_attempt_str:
        next_charge_html = "<div style='color:#888;font-size:13px;margin-bottom:16px'>We'll try again on <strong>" + next_attempt_str + "</strong>.</div>"

    html = """
<div style="font-family:sans-serif;max-width:500px;margin:0 auto;background:#0a0a0a;color:white;padding:32px;border-radius:16px">
  <div style="font-size:32px;font-weight:900;letter-spacing:4px;color:#C026D3;margin-bottom:8px">BEATFINDER</div>
  <div style="color:""" + headline_color + """;font-size:18px;font-weight:700;margin-bottom:12px">""" + headline + """</div>
  <div style="color:#aaa;margin-bottom:16px">Hi """ + name + """, your """ + plan_label + """ subscription payment was declined. This usually means your card has expired, been replaced, or your bank blocked the charge.</div>
  """ + urgency + next_charge_html + """
  <div style="color:white;font-weight:700;margin-bottom:12px">To fix this:</div>
  <ol style="color:#aaa;line-height:1.7;padding-left:20px;margin-bottom:24px">
    <li>Sign in to BeatFinder</li>
    <li>Go to your Profile → Settings (gear icon)</li>
    <li>Tap "Manage Subscription"</li>
    <li>Update your card on Stripe's secure portal</li>
  </ol>
  <div style="color:#555;font-size:12px;margin-top:24px">Questions or need help? trellitheproducer@gmail.com</div>
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
                    "subject": "Payment failed for your BeatFinder " + plan_label + " subscription",
                    "html":    html,
                },
            )
        return r.status_code == 200
    except Exception as e:
        print("[Email] Failed to send payment-failed email: " + str(e))
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


# ── Customer Portal ───────────────────────────────────────────────────────────
# Opens Stripe's hosted Customer Portal so subscribers can self-serve:
#   • Cancel their subscription
#   • Update card / payment method
#   • Download invoices
#   • View billing history
# Saves us from having to build and maintain any of those screens ourselves,
# and means we get fewer "please cancel my account" support emails.

@router.post("/customer-portal")
async def create_customer_portal(
    request: Request,
    user=Depends(get_current_user),
):
    """Returns a one-time portal URL the frontend redirects to."""
    customer_id = user.get("stripe_customer_id", "")

    # If we don't have a customer ID stored, try to find one by looking up
    # the customer by email on Stripe. This covers the gap for users whose
    # subscription was created before we started storing customer_id on
    # the webhook (see plan_fields update).
    if not customer_id:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                lookup = await client.get(
                    "https://api.stripe.com/v1/customers",
                    auth=(STRIPE_SECRET, ""),
                    params={"email": user.get("email", ""), "limit": 1},
                )
                lookup_data = lookup.json()
                items = lookup_data.get("data", [])
                if items:
                    customer_id = items[0].get("id", "")
                    # Backfill onto the user record so we don't have to do
                    # this Stripe API lookup on every portal open.
                    if customer_id:
                        db = request.app.state.db
                        await db.users.update_one(
                            {"_id": user["_id"]},
                            {"$set": {"stripe_customer_id": customer_id}},
                        )
        except Exception as e:
            print("[Stripe] customer lookup by email failed: " + str(e))

    if not customer_id:
        # User has no Stripe customer record — they've never subscribed.
        # Return a friendly error the frontend can show.
        raise HTTPException(
            status_code=400,
            detail="No subscription found on your account. Upgrade to a paid plan first."
        )

    # Create the portal session. Stripe gives us a one-time URL that
    # expires after a few minutes — frontend should redirect immediately.
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.stripe.com/v1/billing_portal/sessions",
            auth=(STRIPE_SECRET, ""),
            data={
                "customer":   customer_id,
                "return_url": FRONTEND_URL + "?portal=closed",
            },
        )
        if r.status_code >= 400:
            print("[Stripe] portal session creation failed: " + r.text)
            raise HTTPException(
                status_code=502,
                detail="Couldn't open subscription management. Please try again or contact support.",
            )
        session = r.json()

    return {"portal_url": session.get("url")}


# ── Public pricing endpoint ────────────────────────────────────────────────
# Returns the current price IDs + display strings so the frontend doesn't
# have to hardcode them. When pricing changes in the future, you update
# either this file (display strings) or your Render env vars (price IDs)
# and the frontend picks it up on next load — no code deploy needed.
#
# Public (no auth required) so the signup screen can fetch it before the
# user has an account.
@router.get("/pricing")
async def get_pricing():
    return {
        "subscriptions": {
            "artist": {
                "label":         "Artist Pro",
                "monthly": {
                    "price_id":  ARTIST_PRICE_ID,
                    "amount":    4.99,
                    "currency":  "GBP",
                    "display":   "£4.99/mo",
                },
                "annual": {
                    "price_id":  ARTIST_ANNUAL_PRICE_ID,
                    "amount":    49.99,
                    "currency":  "GBP",
                    "display":   "£49.99/yr",
                    "monthly_equivalent_display": "≈ £4.17/mo",
                },
            },
            "producer": {
                "label":         "Producer Pro",
                "monthly": {
                    "price_id":  PRODUCER_PRICE_ID,
                    "amount":    8.99,
                    "currency":  "GBP",
                    "display":   "£8.99/mo",
                },
                "annual": {
                    "price_id":  PRODUCER_ANNUAL_PRICE_ID,
                    "amount":    89.99,
                    "currency":  "GBP",
                    "display":   "£89.99/yr",
                    "monthly_equivalent_display": "≈ £7.50/mo",
                },
            },
        },
        "leases": {
            "basic":   {"amount": 50.00,  "currency": "GBP", "display": "£50"},
            "premium": {"min": 100,       "max": 500,        "currency": "GBP", "display": "£100–£500"},
        },
        "platform_fee_percent": 1,
    }


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
        # Capture the Stripe customer ID so we can later open the Customer
        # Portal for this user (cancel sub / update card / view invoices).
        # checkout.session.completed has `customer` on the session itself;
        # invoice.payment_succeeded has it on the invoice.
        customer_id   = session.get("customer", "")

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
                    if not customer_id:
                        customer_id = sub_data.get("customer", "")
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
            "subscription_expires_at": expires_dt,
            # Clear any failing-payment flag from a previous declined attempt —
            # successful charge means whatever was wrong is now fixed.
            "payment_failing":         False,
            "payment_failed_at":       None,
            "payment_failed_attempt":  0,
            "payment_failed_next_retry": None,
        }
        # Only set customer_id if we actually have one — avoid wiping a
        # previously-stored value with an empty string on a re-fire.
        if customer_id:
            plan_fields["stripe_customer_id"] = customer_id

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

    # ── Payment failed (declined card / expired card / etc) ───────────────
    elif event.get("type") == "invoice.payment_failed":
        # Stripe will automatically retry the charge a few times over the
        # next ~3 weeks per their Smart Retries logic, then fire
        # customer.subscription.deleted if all attempts fail.
        #
        # Our job here:
        #   1. Mark the user as "payment failing" in our DB
        #   2. Email them with instructions to fix their card via Portal
        #   3. Keep their pro access active for now — Stripe handles the
        #      retry cadence, and the existing customer.subscription.deleted
        #      handler downgrades them when all retries are exhausted.
        invoice         = event["data"]["object"]
        sub_id          = invoice.get("subscription") or ""
        customer_id     = invoice.get("customer") or ""
        attempt_count   = int(invoice.get("attempt_count") or 1)
        next_attempt_ts = invoice.get("next_payment_attempt") or 0
        user_email      = invoice.get("customer_email", "") or ""

        # Fallback: look up customer if no email on invoice
        if not user_email and customer_id:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    cr = await client.get(
                        "https://api.stripe.com/v1/customers/" + customer_id,
                        auth=(STRIPE_SECRET, ""),
                    )
                    user_email = cr.json().get("email", "")
            except Exception as e:
                print("[Stripe] payment_failed customer lookup error: " + str(e))

        if not user_email:
            print("[Stripe] payment_failed: no email, skipping")
            return {"received": True}

        # Mark the user as having a failing payment. We don't downgrade —
        # Stripe will retry and the subscription is still technically active
        # until it's cancelled. This flag is mostly for UI hints / debugging.
        user_doc = await db.users.find_one({"email": user_email})
        if not user_doc:
            print("[Stripe] payment_failed: no user for " + user_email)
            return {"received": True}

        await db.users.update_one(
            {"email": user_email},
            {"$set": {
                "payment_failing":            True,
                "payment_failed_at":          datetime.utcnow(),
                "payment_failed_attempt":     attempt_count,
                "payment_failed_next_retry":  datetime.utcfromtimestamp(next_attempt_ts) if next_attempt_ts else None,
            }}
        )

        plan = user_doc.get("plan", "")
        name = user_doc.get("name", "")

        # Send the warning email — escalates wording at attempts 1/2-3/4+
        sent = await send_payment_failed_email(
            user_email, name, plan, attempt_count, next_attempt_ts
        )
        print(
            "[Stripe] payment_failed for " + user_email +
            " (attempt " + str(attempt_count) + "), email_sent=" + str(sent)
        )

    # ── Payment recovered after a failed attempt ──────────────────────────
    elif event.get("type") == "invoice.payment_succeeded":
        # This event ALREADY fires inside the big upgrade handler at the top
        # of this webhook (in the same condition tree). We separately catch
        # it here ONLY to clear the payment_failing flag if it was set —
        # a successful charge means the user fixed their card.
        # NOTE: the elif at the top of this function already returns by the
        # time we reach here, so this branch is unreachable. Keeping the
        # comment to document the design intent. The flag is cleared in the
        # main success handler instead — see the plan_fields update there.
        pass

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
