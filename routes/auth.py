"""
Auth routes: /api/auth/
"""

from fastapi import APIRouter, HTTPException, Request, Depends, UploadFile, File
from bson import ObjectId
from datetime import datetime

from models import RegisterRequest, LoginRequest, TokenResponse, PlanUpgradeRequest
from pydantic import BaseModel
from typing import Optional
from auth import hash_password, verify_password, create_token, get_current_user

router = APIRouter()


# ── Regex escape helper (used for case-insensitive email lookups) ──
import re as _re
def _re_escape(s: str) -> str:
    return _re.escape(s or "")


# ── Email-normalization migration state ──────────────────────────────
# We backfill normalized_email for all existing users and create a
# (non-unique) index on it the first time any auth endpoint runs after
# deploy. Doing this lazily means we don't need to touch main.py or
# wire it into the FastAPI lifespan event — it just happens silently
# on first traffic post-deploy. The flag in app.state prevents the
# work from running on every request.
async def _ensure_email_normalization_ready(db, app_state):
    """One-shot migration: backfill normalized_email + ensure index."""
    if getattr(app_state, "_bf_email_norm_ready", False):
        return
    # Set flag immediately to avoid concurrent first-requests both running
    # the migration. Worst case: two run in parallel and both no-op (the
    # backfill query filters out already-normalized rows, and create_index
    # is idempotent in Mongo).
    app_state._bf_email_norm_ready = True
    try:
        # Backfill — only touches rows that don't already have a normalized_email
        cursor = db.users.find(
            {"normalized_email": {"$exists": False}},
            {"_id": 1, "email": 1},
        )
        count = 0
        async for u in cursor:
            norm = normalize_email(u.get("email", ""))
            if not norm:
                continue
            await db.users.update_one(
                {"_id": u["_id"]},
                {"$set": {"normalized_email": norm}},
            )
            count += 1
        if count:
            print(f"[Email norm] Backfilled {count} users")

        # UNIQUE index for fast lookups AND database-level dedup protection.
        # Migration order:
        #   1. Check if a non-unique normalized_email index already exists
        #      (from the previous deploy). If yes, drop it.
        #   2. Create the new unique index.
        # If unique creation fails (i.e. a duplicate slipped through that
        # we don't know about), we re-create the non-unique index as a
        # fallback so lookups stay fast — and log loudly so the admin
        # knows to investigate.
        try:
            # Find any existing index on normalized_email
            existing_indexes = await db.users.index_information()
            existing_norm_idx_name = None
            existing_was_unique    = False
            for idx_name, idx_info in existing_indexes.items():
                key = idx_info.get("key", [])
                if len(key) == 1 and key[0][0] == "normalized_email":
                    existing_norm_idx_name = idx_name
                    existing_was_unique    = bool(idx_info.get("unique", False))
                    break

            if existing_norm_idx_name and not existing_was_unique:
                # Non-unique index from previous deploy — drop it so we
                # can replace with the unique version.
                try:
                    await db.users.drop_index(existing_norm_idx_name)
                    print(f"[Email norm] Dropped non-unique index {existing_norm_idx_name}")
                except Exception as drop_err:
                    print(f"[Email norm] Could not drop old index: {drop_err}")

            if existing_was_unique:
                print("[Email norm] Unique index already present")
            else:
                try:
                    await db.users.create_index("normalized_email", unique=True)
                    print("[Email norm] UNIQUE index ensured on users.normalized_email")
                except Exception as unique_err:
                    # Most likely cause: a leftover duplicate that wasn't
                    # cleaned up. Fall back to non-unique so app keeps
                    # working, but log loudly.
                    print(f"[Email norm] ⚠ UNIQUE index FAILED — duplicates may exist: {unique_err}")
                    try:
                        await db.users.create_index("normalized_email")
                        print("[Email norm] Fell back to non-unique index")
                    except Exception as fallback_err:
                        print(f"[Email norm] Fallback index also failed: {fallback_err}")
        except Exception as e:
            print(f"[Email norm] Index management error: {e}")

        # ── Presence index (last_seen_at) ─────────────────────────
        # Used by admin Users panel filters + future "who's online now"
        # queries. Non-unique. Idempotent — create_index is safe to
        # call repeatedly.
        try:
            await db.users.create_index("last_seen_at")
            print("[Presence] Index ensured on users.last_seen_at")
        except Exception as e:
            print(f"[Presence] Could not create last_seen_at index: {e}")

        # ── One-off admin-flag correction ─────────────────────────
        # Legacy: some lifetime accounts had `is_admin: True` set on their
        # MongoDB user doc directly (before LIFETIME_ACCOUNTS existed as
        # the source of truth). The admin Users panel reads OR of both
        # sources so those leftovers still show up as ADMIN. Fix: walk
        # the LIFETIME_ACCOUNTS dict and force the DB flag to match.
        # Idempotent — runs once per process, then short-circuits.
        try:
            for uname, cfg in LIFETIME_ACCOUNTS.items():
                expected = bool(cfg.get("is_admin", False))
                result = await db.users.update_one(
                    {"username": uname, "is_admin": {"$ne": expected}},
                    {"$set": {"is_admin": expected}},
                )
                if result.modified_count > 0:
                    print(f"[Admin flag] Corrected @{uname} → is_admin={expected}")
        except Exception as e:
            print(f"[Admin flag] Correction error (non-fatal): {e}")

        # ── Username normalization backfill ───────────────────────
        # Same pattern as email norm above: store a `normalized_username`
        # field (lowercase) so future case-insensitive lookups (Deploy B)
        # can use it for fast matches. This deploy ONLY adds the field +
        # index. No lookup behavior changes yet.
        try:
            cursor = db.users.find(
                {"username": {"$exists": True}, "normalized_username": {"$exists": False}},
                {"_id": 1, "username": 1},
            )
            count = 0
            async for u in cursor:
                norm_u = normalize_username(u.get("username", ""))
                if not norm_u:
                    continue
                await db.users.update_one(
                    {"_id": u["_id"]},
                    {"$set": {"normalized_username": norm_u}},
                )
                count += 1
            if count:
                print(f"[Username norm] Backfilled {count} users")

            # Non-unique index for fast lookups in Deploy B.
            # We deliberately don't make this UNIQUE in Deploy A — if
            # case-variant duplicates already exist (Mikez + mikez),
            # unique creation would FAIL and the index wouldn't exist.
            # We catch dupes lazily and clean them up before flipping
            # to unique in a follow-up deploy.
            try:
                await db.users.create_index("normalized_username")
                print("[Username norm] Index ensured on users.normalized_username")
            except Exception as e:
                print(f"[Username norm] Could not create index: {e}")

            # Surface any existing case-variant duplicates so we know
            # what to clean up before flipping to UNIQUE.
            try:
                pipeline = [
                    {"$match": {"normalized_username": {"$exists": True, "$ne": ""}}},
                    {"$group": {"_id": "$normalized_username", "count": {"$sum": 1},
                                "usernames": {"$push": "$username"}}},
                    {"$match": {"count": {"$gt": 1}}},
                ]
                dupes = await db.users.aggregate(pipeline).to_list(50)
                for d in dupes:
                    print(f"[Username norm] ⚠ DUPLICATE case-variant '{d['_id']}': {d['usernames']}")
            except Exception as e:
                print(f"[Username norm] Could not scan duplicates: {e}")
        except Exception as e:
            print(f"[Username norm] Backfill error (non-fatal): {e}")
    except Exception as e:
        # Don't break login/register if migration fails — log and move on.
        # The endpoints have a fallback to case-insensitive raw email lookup.
        print(f"[Email norm] Migration error (non-fatal): {e}")


# ── Email normalization ──────────────────────────────────────────────
# Two emails that look different to a naive string compare can deliver
# to the same inbox: "Foo@gmail.com" vs "foo@gmail.com" (case), or
# "f.o.o@gmail.com" vs "foo@gmail.com" (Gmail strips dots), or
# "foo+anything@gmail.com" vs "foo@gmail.com" (Gmail strips +aliases).
#
# Storing AND querying against a normalized form prevents accidental
# duplicate accounts. We keep the user's original email too (for
# display + sending mail) and add a `normalized_email` field that's
# unique. Existing rows get backfilled lazily on startup.
def normalize_email(email: str) -> str:
    """Return a canonical form of an email for uniqueness checks.

    Rules applied to every email:
      • Lowercase the whole thing
      • Strip surrounding whitespace

    Additional rules for Gmail / Googlemail (and only those):
      • Remove all dots from the local part
      • Strip "+suffix" aliases

    Returns "" if the input isn't a recognisable email — callers
    should treat that as invalid and 400.
    """
    if not email or not isinstance(email, str):
        return ""
    s = email.strip().lower()
    if "@" not in s:
        return ""
    local, _, domain = s.rpartition("@")
    if not local or not domain:
        return ""
    # Gmail-specific tricks: dots in local part are ignored, and
    # "+anything" is an alias to the same inbox. Googlemail.com is
    # the same provider with a different domain name.
    if domain in ("gmail.com", "googlemail.com"):
        # Strip alias
        if "+" in local:
            local = local.split("+", 1)[0]
        # Strip dots
        local = local.replace(".", "")
        # Normalize domain to gmail.com for both legacy variants
        domain = "gmail.com"
    return local + "@" + domain


def normalize_username(username: str) -> str:
    """Return a canonical form of a username for uniqueness + lookup.

    Strategy: lowercase + strip whitespace. We keep the user's original
    casing in the `username` field for display ("Trelli" displays as
    "Trelli", not "trelli"), but compare/lookup uses this normalized
    form. Same pattern as Twitter/Instagram.

    Why this matters: without normalization, "Trelli", "trelli", and
    "TRELLI" are three different accounts — duplicate handles slip
    through, /profile/trelli 404s when the canonical handle is
    "Trelli", DMs/mentions/follows all break across cases.
    """
    if not username or not isinstance(username, str):
        return ""
    return username.strip().lower()


# ── Timestamp helper ─────────────────────────────────────────────────
# All datetimes in MongoDB are stored naive-UTC (via datetime.utcnow()).
# When we serialise them with .isoformat() the resulting string has NO
# timezone marker — JavaScript's `new Date(...)` then parses it as LOCAL
# time, which makes a post made 30 seconds ago look 1 hour old in BST
# (or however far off the user's TZ is from UTC).
# This helper appends "Z" so the client correctly treats it as UTC.
def _iso_utc(dt) -> str:
    """Serialise a naive-UTC datetime as an ISO-8601 string with Z suffix.
    Accepts datetime, ISO string, or falsy → returns "" if nothing valid."""
    if not dt:
        return ""
    if hasattr(dt, "isoformat"):
        s = dt.isoformat()
        return s if s.endswith("Z") or "+" in s else s + "Z"
    s = str(dt)
    if not s:
        return ""
    return s if s.endswith("Z") or "+" in s[10:] else s + "Z"


PLANS = {
    "artist":   {"price_gbp": 4.99, "paypal_link": "https://www.paypal.com/paypalme/trellitheproducer/4.99GBP"},
    "producer": {"price_gbp": 8.99, "paypal_link": "https://www.paypal.com/paypalme/trellitheproducer/8.99GBP"},
}


# ── Lifetime accounts — never expire ──────────────────────────────────
# Two sources of truth, merged:
#   1. LIFETIME_ACCOUNTS dict below — hardcoded permanent grants
#      (Trelli/Mikez/HMbarsdat). Can't be revoked through admin UI,
#      acts as a "floor" guaranteeing these accounts are always lifetime.
#   2. db.lifetime_accounts collection — admin-grantable lifetime status.
#      Documents look like:
#        { "_id": "<username>", "plan": "artist" | "producer",
#          "is_admin": false, "granted_at": <datetime>,
#          "granted_by": "<admin_username>" }
#
# _lifetime_config() now checks the hardcoded dict FIRST (fast path),
# then falls back to a DB lookup if not found. The DB lookup is async
# so any synchronous call sites need to be updated — see _lifetime_config
# below and its async variant _lifetime_config_async.
LIFETIME_ACCOUNTS = {
    "Trelli":     {"plan": "producer", "is_admin": True},
    "Mikez":      {"plan": "artist",   "is_admin": False},
    "HMbarsdat":  {"plan": "artist",   "is_admin": False},
}


def _lifetime_config(username: str):
    """Synchronous lifetime check — only consults the hardcoded dict.
    Used by code paths that aren't async (rare). For routes that ARE
    async (most of them), prefer _lifetime_config_async which also
    consults the database for admin-granted lifetimes."""
    if not username:
        return None
    return LIFETIME_ACCOUNTS.get(username)


async def _lifetime_config_async(db, username: str):
    """Async lifetime check — consults both the hardcoded dict AND the
    db.lifetime_accounts collection. Returns the config dict or None.

    Order:
      1. Hardcoded dict (permanent floor — never revoked)
      2. DB collection (admin-grantable, admin-revocable)
    """
    if not username:
        return None
    hard = LIFETIME_ACCOUNTS.get(username)
    if hard:
        return hard
    try:
        doc = await db.lifetime_accounts.find_one({"_id": username})
        if doc:
            return {
                "plan":     doc.get("plan", "artist"),
                "is_admin": bool(doc.get("is_admin", False)),
            }
    except Exception:
        pass
    return None


async def get_effective_plan(db, user: dict) -> str:
    """Return the user's effective plan, applying lifetime overrides.

    Critical: any backend check that reads `user["plan"]` directly will
    miss lifetime-granted access. Lifetime users have plan stored as
    "free" in their user doc but are entitled to "artist" or "producer"
    features via LIFETIME_ACCOUNTS (hardcoded) or db.lifetime_accounts
    (admin-granted).

    Usage:
        if await get_effective_plan(db, user) not in ("artist", "producer"):
            raise HTTPException(403, "Pro plan required")

    Returns the plan string: "artist" / "producer" / "free".
    """
    if not user:
        return "free"
    cfg = await _lifetime_config_async(db, user.get("username", ""))
    if cfg:
        return cfg.get("plan", "artist")
    return user.get("plan", "free")


async def has_active_pro(db, user: dict, *, require_producer: bool = False) -> bool:
    """Whether the user is entitled to Pro features (subscription OR lifetime).

    require_producer=True means producer-tier specifically (e.g. selling
    beats requires producer, not just artist).

    Lifetime accounts ALWAYS pass — they're entitled regardless of
    subscriptionActive (which is False on their underlying user doc).
    """
    if not user:
        return False
    eff = await get_effective_plan(db, user)
    # Lifetime path: if the user is in LIFETIME_ACCOUNTS or db.lifetime_accounts
    # then `eff` reflects the override and we trust it directly.
    cfg = await _lifetime_config_async(db, user.get("username", ""))
    if cfg:
        if require_producer:
            return eff == "producer"
        return eff in ("artist", "producer")
    # Regular path: stored plan + active subscription check.
    if require_producer:
        if eff != "producer":
            return False
    else:
        if eff not in ("artist", "producer"):
            return False
    return bool(user.get("subscriptionActive", False))



def _public(user: dict) -> dict:
    from datetime import timezone

    # ── Online status ────────────────────────────────────────────
    # "Online" = last_seen_at within the last 2 minutes. We compute
    # it here so every endpoint that calls _public gets it for free.
    # last_seen_at is stored in MongoDB as a naive UTC datetime and
    # updated by /heartbeat at ~60s intervals from the frontend.
    online_window_seconds = 120
    last_seen = user.get("last_seen_at")
    is_online = False
    last_seen_iso = ""
    if isinstance(last_seen, datetime):
        delta = (datetime.utcnow() - last_seen).total_seconds()
        is_online = 0 <= delta <= online_window_seconds
        last_seen_iso = _iso_utc(last_seen)

    username = user.get("username", "")
    # Prefer an override injected by the async endpoint (which has access
    # to db.lifetime_accounts). Falls back to the synchronous hardcoded
    # check so non-async call sites still work for the OG lifetime users.
    cfg = user.get("__lifetime_override__")
    if not cfg:
        cfg = _lifetime_config(username)
    if cfg:
        return {
            "id":                    str(user["_id"]),
            "name":                  user.get("name", ""),
            "email":                 user.get("email", ""),
            "plan":                  cfg["plan"],
            "username":              username,
            "bio":                   user.get("bio", ""),
            "location":              user.get("location", ""),
            "instagram":             user.get("instagram", ""),
            "tiktok":                user.get("tiktok", ""),
            "youtube":               user.get("youtube", ""),
            "spotify":               user.get("spotify", ""),
            "website":               user.get("website", ""),
            "avatarColor":           user.get("avatarColor", ""),
            "avatarUrl":             user.get("avatarUrl", ""),
            "appleMusic":            user.get("appleMusic", ""),
            "headerUrl":             user.get("headerUrl", ""),
            "is_admin":              cfg["is_admin"],
            "created_at":            user.get("created_at", "").isoformat() if user.get("created_at") else None,
            "subscriptionActive":    True,
            "subscriptionExpiresAt": None,
            "billingInterval":       "lifetime",
            "terms_accepted_version": user.get("terms_accepted_version", ""),
            "beatArtworkUrl":         user.get("beatArtworkUrl", ""),
            "is_online":              is_online,
            "last_seen_at":           last_seen_iso,
        }

    expires_at = user.get("subscription_expires_at")
    sub_active = False
    if expires_at:
        if isinstance(expires_at, datetime):
            sub_active = expires_at > datetime.utcnow()
        else:
            try:
                sub_active = float(expires_at) > datetime.utcnow().timestamp()
            except Exception:
                sub_active = False
    plan = user.get("plan", "free")
    if plan == "free":
        sub_active = False
    # Expired paid plan — return plan as free for feature gating
    effective_plan = plan if sub_active else "free"
    return {
        "id":                    str(user["_id"]),
        "name":                  user.get("name", ""),
        "email":                 user.get("email", ""),
        "plan":                  effective_plan,
        "username":              user.get("username", ""),
        "bio":                   user.get("bio", ""),
        "location":              user.get("location", ""),
        "instagram":             user.get("instagram", ""),
        "tiktok":                user.get("tiktok", ""),
        "youtube":               user.get("youtube", ""),
        "spotify":               user.get("spotify", ""),
        "website":               user.get("website", ""),
        "avatarColor":           user.get("avatarColor", ""),
        "avatarUrl":             user.get("avatarUrl", ""),
        "appleMusic":            user.get("appleMusic", ""),
        "headerUrl":             user.get("headerUrl", ""),
        "is_admin":              user.get("is_admin", False),
        "created_at":            user.get("created_at", "").isoformat() if user.get("created_at") else None,
        "subscriptionActive":    sub_active,
        "subscriptionExpiresAt": expires_at.isoformat() if isinstance(expires_at, datetime) else (str(expires_at) if expires_at else None),
        "billingInterval":       user.get("billing_interval", "monthly"),
        "terms_accepted_version": user.get("terms_accepted_version", ""),
        "beatArtworkUrl":         user.get("beatArtworkUrl", ""),
        "is_online":              is_online,
        "last_seen_at":           last_seen_iso,
    }


# ── Register ──────────────────────────────────────────────────────
@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, request: Request):
    db = request.app.state.db
    # Run one-time email-normalization migration if needed
    await _ensure_email_normalization_ready(db, request.app.state)

    # Enforce server-side validation — frontend has the same checks but
    # API clients (or a malicious user bypassing the UI) could otherwise
    # create accounts with trivially-guessable passwords.
    if not body.password or len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    if len(body.password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Password too long. Maximum 72 characters.")
    if not body.email or "@" not in body.email or "." not in body.email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Please provide a valid email address.")
    if not body.name or not body.name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")

    # Normalize for uniqueness — this catches case differences AND
    # Gmail dot/+ tricks. We check both the normalized field (new
    # accounts) and the raw email (legacy accounts pre-normalization).
    norm = normalize_email(body.email)
    if not norm:
        raise HTTPException(status_code=400, detail="Please provide a valid email address.")
    existing = await db.users.find_one({
        "$or": [
            {"normalized_email": norm},
            {"email": {"$regex": "^" + _re_escape(body.email) + "$", "$options": "i"}},
        ]
    })
    if existing:
        # Don't reveal whether the existing one is deleted or active —
        # tell the user clearly that this email is in use.
        raise HTTPException(status_code=409, detail="Email already registered")

    user_id = str(ObjectId())
    user = {
        "_id":              user_id,
        "name":             body.name,
        "email":            body.email,
        "normalized_email": norm,
        "password":         hash_password(body.password),
        "plan":             "free",
        "is_admin":         False,
        "bio":              "",
        "location":         "",
        "created_at":       datetime.utcnow(),
    }
    await db.users.insert_one(user)
    token = create_token(user_id, body.email)
    return {"access_token": token, "user": _public(user)}


# ── Login ─────────────────────────────────────────────────────────
@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request):
    db   = request.app.state.db
    # Run one-time email-normalization migration if needed
    await _ensure_email_normalization_ready(db, request.app.state)
    # Allow users to log in regardless of email casing or Gmail
    # dot/+ variations. Try normalized first; fall back to a
    # case-insensitive exact match for legacy rows pre-backfill.
    norm = normalize_email(body.email)
    user = None
    if norm:
        user = await db.users.find_one({"normalized_email": norm})
    if not user and body.email:
        user = await db.users.find_one({
            "email": {"$regex": "^" + _re_escape(body.email) + "$", "$options": "i"}
        })
    if not user or not verify_password(body.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_token(str(user["_id"]), user["email"])
    # Inject lifetime override so DB-granted lifetime users get the
    # right plan returned in the login response.
    user["__lifetime_override__"] = await _lifetime_config_async(db, user.get("username", ""))
    return {"access_token": token, "user": _public(user)}


# ── Me ────────────────────────────────────────────────────────────
@router.get("/me")
async def me(request: Request, user=Depends(get_current_user)):
    db = request.app.state.db
    user["__lifetime_override__"] = await _lifetime_config_async(db, user.get("username", ""))
    return _public(user)


# ── Heartbeat (presence) ─────────────────────────────────────────
# Frontend pings this every ~60s while the app is open and visible.
# We update last_seen_at on the user row; other endpoints compute
# is_online from that timestamp via _public(). Fire-and-forget on
# the client side — failures are not surfaced to the user.
@router.post("/heartbeat")
async def heartbeat(request: Request, user=Depends(get_current_user)):
    from db_helpers import update_user_by_id
    db = request.app.state.db
    # Touch the timestamp. Deliberately bypass other validation —
    # this is the hottest auth endpoint in the app, runs constantly.
    await update_user_by_id(db, user["_id"], {
        "$set": {"last_seen_at": datetime.utcnow()},
    })
    return {"ok": True}


# ── Accept Terms & Conditions ────────────────────────────────────
# Persists the user's acceptance of a specific Terms version.
# Versioning lets us re-prompt all users when the legal text changes:
# bump TERMS_VERSION on the frontend and every user is automatically
# shown the new modal on next load (because their stored version no
# longer matches).
class AcceptTermsRequest(BaseModel):
    version: str

@router.post("/accept-terms")
async def accept_terms(body: AcceptTermsRequest, request: Request, user=Depends(get_current_user)):
    if not body.version or not isinstance(body.version, str) or len(body.version) > 32:
        raise HTTPException(status_code=400, detail="Invalid version")
    db = request.app.state.db
    # Capture client IP + user agent for the audit trail. These give us
    # defensible evidence of who accepted the Terms, from where, and when
    # — useful if a user later disputes consent or under GDPR challenges.
    client_ip = ""
    try:
        # Trust X-Forwarded-For from Render's reverse proxy
        fwd = request.headers.get("x-forwarded-for", "")
        client_ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")
    except Exception:
        client_ip = ""
    user_agent = request.headers.get("user-agent", "")[:512]
    now = datetime.utcnow()
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "terms_accepted_version":    body.version,
                "terms_accepted_at":         now,
                "terms_accepted_ip":         client_ip,
                "terms_accepted_user_agent": user_agent,
            }
        },
    )
    # Also append to a permanent acceptance history collection so we keep
    # every acceptance event forever, even if the user later accepts a
    # newer version (which would otherwise overwrite the user document).
    try:
        await db.terms_acceptances.insert_one({
            "user_id":    user["_id"],
            "username":   user.get("username", ""),
            "email":      user.get("email", ""),
            "version":    body.version,
            "accepted_at": now,
            "ip":         client_ip,
            "user_agent": user_agent,
        })
    except Exception:
        # Non-fatal — main update succeeded
        pass
    return {
        "success": True,
        "version": body.version,
        "accepted_at": now.isoformat(),
    }


# ── Upgrade plan ──────────────────────────────────────────────────
@router.post("/upgrade")
async def upgrade_plan(body: PlanUpgradeRequest, request: Request, user=Depends(get_current_user)):
    if body.plan not in PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan")
    plan_info = PLANS[body.plan]
    return {
        "plan":        body.plan,
        "price_gbp":   plan_info["price_gbp"],
        "paypal_link": plan_info["paypal_link"],
    }


# ── GDPR: Export My Data (Article 15 — right of access) ──────────
# Returns a JSON snapshot of everything we hold on this user, suitable
# for delivery to the user as a downloadable file. Sensitive fields
# (password hash, internal admin flags) are excluded.
@router.get("/export-my-data")
async def export_my_data(request: Request, user=Depends(get_current_user)):
    db = request.app.state.db
    uid = user["_id"]
    uid_str = str(uid)

    def _clean_doc(d: dict) -> dict:
        """Convert ObjectId/datetime to string; strip internal fields."""
        if not d:
            return d
        out = {}
        for k, v in d.items():
            if k in ("password", "hashed_password", "stripe_secret_key", "_id"):
                continue
            if isinstance(v, ObjectId):
                out[k] = str(v)
            elif isinstance(v, datetime):
                out[k] = v.isoformat()
            elif isinstance(v, list):
                out[k] = [
                    _clean_doc(x) if isinstance(x, dict) else (str(x) if isinstance(x, ObjectId) else x)
                    for x in v
                ]
            elif isinstance(v, dict):
                out[k] = _clean_doc(v)
            else:
                out[k] = v
        return out

    profile = _clean_doc(user)

    # Gather all collections containing this user's data
    beats             = await db.beats.find({"producer_id": uid_str}).to_list(length=None) if "beats" in await db.list_collection_names() else []
    leases_bought     = await db.producer_leases.find({"user_id": uid_str}).to_list(length=None) if "producer_leases" in await db.list_collection_names() else []
    leases_sold       = await db.producer_leases.find({"producer_id": uid_str}).to_list(length=None) if "producer_leases" in await db.list_collection_names() else []
    free_licences     = await db.free_licence_agreements.find({"user_id": uid_str}).to_list(length=None) if "free_licence_agreements" in await db.list_collection_names() else []
    saved_lyrics      = await db.lyrics.find({"user_id": uid_str}).to_list(length=None) if "lyrics" in await db.list_collection_names() else []
    messages_sent     = await db.messages.find({"sender_id": uid_str}).to_list(length=None) if "messages" in await db.list_collection_names() else []
    messages_received = await db.messages.find({"recipient_id": uid_str}).to_list(length=None) if "messages" in await db.list_collection_names() else []
    notifications     = await db.notifications.find({"user_id": uid_str}).to_list(length=None) if "notifications" in await db.list_collection_names() else []

    export = {
        "export_metadata": {
            "exported_at":           datetime.utcnow().isoformat(),
            "exported_for_user_id":  uid_str,
            "data_controller":       "BeatFinder",
            "contact":               "support@beatfinder.co.uk",
            "lawful_basis":          "UK GDPR Article 15 — Right of Access",
            "format_note":           "JSON. Some fields (password hash, internal admin flags) are excluded for security.",
        },
        "profile":           profile,
        "beats_uploaded":    [_clean_doc(b) for b in beats],
        "leases_purchased":  [_clean_doc(l) for l in leases_bought],
        "leases_sold":       [_clean_doc(l) for l in leases_sold],
        "free_licences":     [_clean_doc(l) for l in free_licences],
        "saved_lyrics":      [_clean_doc(l) for l in saved_lyrics],
        "messages_sent":     [_clean_doc(m) for m in messages_sent],
        "messages_received": [_clean_doc(m) for m in messages_received],
        "notifications":     [_clean_doc(n) for n in notifications],
    }
    return export


# ── GDPR: Delete My Account (Article 17 — right to erasure) ──────
# Soft-deletes the user: anonymises identifiable fields and marks
# the account as deleted. A scheduled job (run manually for now)
# can hard-purge accounts that have been soft-deleted for >30 days.
# We keep some records (e.g. lease contracts already sold to other
# users) for legal reasons (UK tax law requires 6-year retention,
# and licensees retain valid leases regardless of the seller's
# account status).
class DeleteAccountRequest(BaseModel):
    confirm_email: str
    reason: Optional[str] = None

@router.post("/delete-my-account")
async def delete_my_account(body: DeleteAccountRequest, request: Request, user=Depends(get_current_user)):
    # Verify the user typed their own email to confirm intent
    if (body.confirm_email or "").strip().lower() != (user.get("email") or "").lower():
        raise HTTPException(status_code=400, detail="Confirmation email does not match account email")

    db = request.app.state.db
    uid = user["_id"]
    uid_str = str(uid)

    now = datetime.utcnow()
    anon_email = f"deleted-{uid_str}@deleted.beatfinder.invalid"
    anon_username = f"deleted_user_{uid_str[:8]}"

    # Anonymise the user record (keep _id for foreign-key integrity)
    await db.users.update_one(
        {"_id": uid},
        {
            "$set": {
                "deleted":               True,
                "deleted_at":            now,
                "deletion_reason":       (body.reason or "")[:500],
                "email":                 anon_email,
                "username":              anon_username,
                "name":                  "Deleted user",
                "bio":                   "",
                "location":              "",
                "instagram":             "",
                "tiktok":                "",
                "youtube":               "",
                "spotify":               "",
                "website":               "",
                "avatarUrl":             "",
                "headerUrl":             "",
                "appleMusic":            "",
                "stripe_customer_id":    "",
                "subscription_expires_at": None,
                "plan":                  "free",
            },
            "$unset": {
                "password": "",
                "hashed_password": "",
            },
        },
    )

    # Remove their uploaded beats from public visibility
    if "beats" in await db.list_collection_names():
        await db.beats.update_many(
            {"producer_id": uid_str},
            {"$set": {"deleted": True, "deleted_at": now, "hidden": True}},
        )

    # Soft-delete personal items (lyrics, messages) — kept for foreign keys
    if "lyrics" in await db.list_collection_names():
        await db.lyrics.delete_many({"user_id": uid_str})
    if "free_licence_agreements" in await db.list_collection_names():
        await db.free_licence_agreements.delete_many({"user_id": uid_str})

    # Note: leases (purchased or sold) are intentionally KEPT —
    # legal/tax retention (6 years HMRC) + buyers retain valid licences.

    return {
        "success": True,
        "deleted_at": now.isoformat(),
        "note": "Your account has been anonymised. Records required for legal compliance (transaction history, lease contracts) are retained under Schedule 2 of the UK GDPR. All personally identifiable fields have been removed. Backups will fully purge within 30 days.",
    }


# ── Set username ──────────────────────────────────────────────────
class UsernameRequest(BaseModel):
    username: str

@router.post("/set-username")
async def set_username(body: UsernameRequest, request: Request, user=Depends(get_current_user)):
    username = body.username.strip()
    if not username or len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
    if len(username) > 30:
        raise HTTPException(status_code=400, detail="Username must be under 30 characters")

    import re
    if not re.match(r"^[a-zA-Z0-9_.]+$", username):
        raise HTTPException(status_code=400, detail="Username can only contain letters, numbers, dots and underscores")

    db = request.app.state.db
    # Case-insensitive uniqueness: even though `username` is stored
    # with the original casing for display, two users can't claim
    # case-variants of the same handle. Check the normalized form.
    norm_u = normalize_username(username)
    existing = await db.users.find_one({
        "$or": [
            {"normalized_username": norm_u},
            # Fallback for legacy rows not yet backfilled
            {"username": {"$regex": "^" + _re_escape(username) + "$", "$options": "i"}},
        ]
    })
    if existing and str(existing["_id"]) != str(user["_id"]):
        raise HTTPException(status_code=409, detail="Username already taken")

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "username": username,                  # original casing for display
            "normalized_username": norm_u,         # lowercase for lookup/uniqueness
        }},
    )
    return {"success": True, "username": username}


# ── Save bio ──────────────────────────────────────────────────────
class BioRequest(BaseModel):
    bio: str

@router.post("/bio")
async def save_bio(body: BioRequest, request: Request, user=Depends(get_current_user)):
    bio = body.bio.strip()[:250]
    db  = request.app.state.db
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"bio": bio}})
    return {"success": True, "bio": bio}


# ── Update full profile (name, location, socials, bio) ────────────
class ProfileUpdateRequest(BaseModel):
    name:        Optional[str] = None
    location:    Optional[str] = None
    bio:         Optional[str] = None
    instagram:   Optional[str] = None
    tiktok:      Optional[str] = None
    youtube:     Optional[str] = None
    spotify:     Optional[str] = None
    appleMusic:  Optional[str] = None
    website:     Optional[str] = None
    avatarColor: Optional[str] = None
    avatarUrl:   Optional[str] = None
    headerUrl:   Optional[str] = None

@router.post("/profile/update")
async def update_profile(body: ProfileUpdateRequest, request: Request, user=Depends(get_current_user)):
    db     = request.app.state.db
    fields = {}
    if body.name        is not None: fields["name"]        = body.name.strip()[:80]
    if body.location    is not None: fields["location"]    = body.location.strip()[:100]
    if body.bio         is not None: fields["bio"]         = body.bio.strip()[:250]
    if body.instagram   is not None: fields["instagram"]   = body.instagram.strip()[:200]
    if body.tiktok      is not None: fields["tiktok"]      = body.tiktok.strip()[:200]
    if body.youtube     is not None: fields["youtube"]     = body.youtube.strip()[:200]
    if body.spotify     is not None: fields["spotify"]     = body.spotify.strip()[:200]
    if body.appleMusic  is not None: fields["appleMusic"]  = body.appleMusic.strip()[:200]
    if body.website     is not None: fields["website"]     = body.website.strip()[:200]
    if body.avatarColor is not None: fields["avatarColor"] = body.avatarColor[:200]
    if body.avatarUrl   is not None: fields["avatarUrl"]   = body.avatarUrl[:500]
    if body.headerUrl   is not None: fields["headerUrl"]   = body.headerUrl[:500]

    if fields:
        await db.users.update_one({"_id": user["_id"]}, {"$set": fields})
    return {"success": True, "updated": list(fields.keys())}


# ── Search users by username ──────────────────────────────────────
@router.get("/search")
async def search_users(q: str, request: Request):
    if not q or len(q.strip()) < 2:
        return []
    db      = request.app.state.db
    pattern = {"$regex": q.strip(), "$options": "i"}
    docs    = await db.users.find(
        {"username": pattern},
        {"password": 0}
    ).limit(20).to_list(20)

    # Apply lifetime override so lifetime accounts surface their correct
    # plan in search results too. We batch-fetch the DB-granted lifetime
    # accounts upfront so this stays O(1) DB calls regardless of result
    # count.
    db_lifetime_docs = await db.lifetime_accounts.find({}).to_list(500)
    db_lifetime_map = {d["_id"]: d for d in db_lifetime_docs}

    def _row(d):
        uname = d.get("username", "")
        cfg = LIFETIME_ACCOUNTS.get(uname) or (
            {"plan": db_lifetime_map[uname].get("plan", "artist"),
             "is_admin": bool(db_lifetime_map[uname].get("is_admin", False))}
            if uname in db_lifetime_map else None
        )
        # Online status from last_seen_at — same 2-min window as _public()
        is_online = False
        last_seen = d.get("last_seen_at")
        if isinstance(last_seen, datetime):
            is_online = 0 <= (datetime.utcnow() - last_seen).total_seconds() <= 120
        return {
            "username":  uname,
            "name":      d.get("name", ""),
            "plan":      cfg["plan"] if cfg else d.get("plan", "free"),
            "bio":       d.get("bio", ""),
            "avatarUrl": d.get("avatarUrl", ""),
            "is_online": is_online,
        }
    return [_row(d) for d in docs if d.get("username")]


# ── Get public profile ────────────────────────────────────────────
@router.get("/profile/{username}")
async def get_public_profile(username: str, request: Request, _user: str = ""):
    db   = request.app.state.db
    user = await db.users.find_one({"username": username})
    if not user:
        raise HTTPException(status_code=404, detail="Profile not found")

    user_id = str(user["_id"])

    # Beats
    beats = await db.producer_beats.find(
        {"producer_id": user_id}
    ).sort("uploaded_at", -1).to_list(50)

    # Follower/following counts
    follower_count  = await db.follows.count_documents({"following_id": user_id})
    following_count = await db.follows.count_documents({"follower_id":  user_id})

    play_count  = sum(b.get("playCount", 0) for b in beats)
    track_count = await db.artist_tracks.count_documents({"artist_id": user_id})

    # Lifetime accounts override the stored plan value — without this,
    # a user who got marked lifetime in the LIFETIME_ACCOUNTS dict but
    # still has plan:"free" in the DB shows as a free account on their
    # public profile (no Pro tick, no plan chip).
    lifetime_cfg = await _lifetime_config_async(db, user.get("username", ""))
    effective_plan = lifetime_cfg["plan"] if lifetime_cfg else user.get("plan", "free")

    return {
        "username":       user.get("username"),
        "name":           user.get("name"),
        "plan":           effective_plan,
        "bio":            user.get("bio", ""),
        "location":       user.get("location", ""),
        "instagram":      user.get("instagram", ""),
        "tiktok":         user.get("tiktok", ""),
        "youtube":        user.get("youtube", ""),
        "spotify":        user.get("spotify", ""),
        "website":        user.get("website", ""),
        "avatarColor":    user.get("avatarColor", ""),
        "avatarUrl":      user.get("avatarUrl", ""),
        "appleMusic":     user.get("appleMusic", ""),
        "headerUrl":      user.get("headerUrl", ""),
        "joined":         user.get("created_at", "").isoformat() if user.get("created_at") else "",
        "followerCount":  follower_count,
        "followingCount": following_count,
        "playCount":      play_count,
        "trackCount":     track_count,
        "isFollowing":    False,
        "beats": [
            {
                "id":                str(b["_id"]),
                "title":             b.get("title"),
                "genre":             b.get("genre"),
                "price":             b.get("price", "free"),
                "url":               b.get("url"),
                "downloads":         b.get("downloads", 0),
                "playCount":         b.get("playCount", 0),
                "producer":          user.get("name", user.get("username", "Unknown")),
                "producer_username": user.get("username", ""),
                "producer_avatar":   user.get("avatarUrl", ""),
                "description":       b.get("description", ""),
                "bpm":               b.get("bpm", 0),
                "key":               b.get("key", ""),
                "preview_start":     b.get("preview_start", 0),
                # Two-tier lease fields — required by frontend to render Basic +
                # Premium tier buttons. Defaults keep legacy beats working.
                "basic_lease_price":   b.get("basic_lease_price", 50 if b.get("price", "free") != "free" else 0),
                "premium_lease_price": b.get("premium_lease_price", 0),
                "premium_sold":        bool(b.get("premium_sold", False)),
                "premium_sold_to":     b.get("premium_sold_to"),
            }
            for b in beats
        ],
    }


# ── Subscription status (called after Stripe redirect + on app load) ─
@router.get("/subscription-status")
async def subscription_status(request: Request, user=Depends(get_current_user)):
    db = request.app.state.db
    # Lifetime accounts — never auto-downgrade. Uses both hardcoded dict
    # and DB-granted lifetime list.
    cfg = await _lifetime_config_async(db, user.get("username", ""))
    if cfg:
        return {
            "plan":                  cfg["plan"],
            "subscriptionActive":    True,
            "subscriptionExpiresAt": None,
            "billingInterval":       "lifetime",
        }
    db         = request.app.state.db
    expires_at = user.get("subscription_expires_at")
    plan       = user.get("plan", "free")
    sub_active = False
    if expires_at and plan != "free":
        if isinstance(expires_at, datetime):
            sub_active = expires_at > datetime.utcnow()
        else:
            try:
                sub_active = float(expires_at) > datetime.utcnow().timestamp()
            except Exception:
                sub_active = False
    # Auto-downgrade expired subscriptions
    if not sub_active and plan != "free":
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"plan": "free", "isPro": False, "isArtistPro": False}}
        )
        plan = "free"
    exp_str = None
    if expires_at:
        exp_str = expires_at.isoformat() if isinstance(expires_at, datetime) else str(expires_at)
    return {
        "plan":                  plan,
        "subscriptionActive":    sub_active,
        "subscriptionExpiresAt": exp_str,
        "billingInterval":       user.get("billing_interval", "monthly"),
    }


# ── Get public profile (authenticated — includes isFollowing) ─────
@router.get("/profile-auth/{username}")
async def get_public_profile_auth(username: str, request: Request, current_user=Depends(get_current_user)):
    db   = request.app.state.db
    user = await db.users.find_one({"username": username})
    if not user:
        raise HTTPException(status_code=404, detail="Profile not found")

    user_id = str(user["_id"])

    beats = await db.producer_beats.find(
        {"producer_id": user_id}
    ).sort("uploaded_at", -1).to_list(50)

    follower_count  = await db.follows.count_documents({"following_id": user_id})
    following_count = await db.follows.count_documents({"follower_id":  user_id})

    # Check if current user follows this profile
    is_following = await db.follows.find_one({
        "follower_id":  str(current_user["_id"]),
        "following_id": user_id,
    }) is not None

    # Check the reverse: does this profile follow the current user
    # back? Needed for the "mutual follow only" gate on the public
    # profile's Message button — DMs are only allowed between users
    # who follow each other both ways.
    is_followed_by = await db.follows.find_one({
        "follower_id":  user_id,
        "following_id": str(current_user["_id"]),
    }) is not None

    play_count  = sum(b.get("playCount", 0) for b in beats)
    track_count = await db.artist_tracks.count_documents({"artist_id": user_id})

    # Lifetime accounts: override the plan + force subscriptionActive=true
    # regardless of any stored subscription_expires_at. Without this they
    # display as Free on their public profile (no Pro tick / no plan chip).
    lifetime_cfg = await _lifetime_config_async(db, user.get("username", ""))
    if lifetime_cfg:
        effective_plan = lifetime_cfg["plan"]
        sub_active     = True
        expires_at     = None
    else:
        effective_plan = user.get("plan", "free")
        expires_at = user.get("subscription_expires_at")
        sub_active = False
        if expires_at and effective_plan != "free":
            if isinstance(expires_at, datetime):
                sub_active = expires_at > datetime.utcnow()
            else:
                try:
                    sub_active = float(expires_at) > datetime.utcnow().timestamp()
                except Exception:
                    sub_active = False

    return {
        "username":              user.get("username"),
        "name":                  user.get("name"),
        "plan":                  effective_plan,
        "bio":                   user.get("bio", ""),
        "location":              user.get("location", ""),
        "instagram":             user.get("instagram", ""),
        "tiktok":                user.get("tiktok", ""),
        "youtube":               user.get("youtube", ""),
        "spotify":               user.get("spotify", ""),
        "appleMusic":            user.get("appleMusic", ""),
        "headerUrl":             user.get("headerUrl", ""),
        "website":               user.get("website", ""),
        "avatarUrl":             user.get("avatarUrl", ""),
        "avatarColor":           user.get("avatarColor", ""),
        "joined":                user.get("created_at", "").isoformat() if user.get("created_at") else "",
        "followerCount":         follower_count,
        "followingCount":        following_count,
        "playCount":             play_count,
        "trackCount":            track_count,
        "subscriptionActive":    sub_active,
        "subscriptionExpiresAt": expires_at.isoformat() if isinstance(expires_at, datetime) else (str(expires_at) if expires_at else None),
        "billingInterval":       user.get("billing_interval", "monthly"),
        "isFollowing":           is_following,
        "isFollowedBy":          is_followed_by,
        "beats": [
            {
                "id":                str(b["_id"]),
                "title":             b.get("title"),
                "genre":             b.get("genre"),
                "price":             b.get("price", "free"),
                "url":               b.get("url"),
                "downloads":         b.get("downloads", 0),
                "playCount":         b.get("playCount", 0),
                "producer":          user.get("name", user.get("username", "Unknown")),
                "producer_username": user.get("username", ""),
                "producer_avatar":   user.get("avatarUrl", ""),
                "description":       b.get("description", ""),
                "bpm":               b.get("bpm", 0),
                "key":               b.get("key", ""),
                "preview_start":     b.get("preview_start", 0),
                # Two-tier lease fields — required by frontend to render Basic +
                # Premium tier buttons. Defaults keep legacy beats working.
                "basic_lease_price":   b.get("basic_lease_price", 50 if b.get("price", "free") != "free" else 0),
                "premium_lease_price": b.get("premium_lease_price", 0),
                "premium_sold":        bool(b.get("premium_sold", False)),
                "premium_sold_to":     b.get("premium_sold_to"),
            }
            for b in beats
        ],
    }


# ── Followers list ───────────────────────────────────────────────
@router.get("/followers/{username}")
async def get_followers(username: str, request: Request):
    db     = request.app.state.db
    target = await db.users.find_one({"username": username})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    target_id   = str(target["_id"])
    follow_docs = await db.follows.find({"following_id": target_id}).to_list(500)
    follower_ids = [f["follower_id"] for f in follow_docs]
    if not follower_ids:
        return []
    users = await db.users.find({"_id": {"$in": follower_ids}}, {"password": 0}).to_list(500)
    # Apply lifetime override so accounts on the lifetime list show their
    # correct plan (and therefore their badges) in this listing. Without
    # this, lifetime users with plan:"free" in the DB show as Free.
    # Batch-fetch DB-granted lifetimes upfront so we stay O(1) DB calls.
    db_lifetime_docs = await db.lifetime_accounts.find({}).to_list(500)
    db_lifetime_map = {d["_id"]: d for d in db_lifetime_docs}
    def _row(u):
        uname = u.get("username", "")
        cfg = LIFETIME_ACCOUNTS.get(uname) or (
            {"plan": db_lifetime_map[uname].get("plan", "artist")}
            if uname in db_lifetime_map else None
        )
        is_online = False
        last_seen = u.get("last_seen_at")
        if isinstance(last_seen, datetime):
            is_online = 0 <= (datetime.utcnow() - last_seen).total_seconds() <= 120
        return {
            "username":  uname,
            "name":      u.get("name", ""),
            "avatarUrl": u.get("avatarUrl", ""),
            "plan":      cfg["plan"] if cfg else u.get("plan", "free"),
            "is_online": is_online,
        }
    return [_row(u) for u in users]


# ── Following list ───────────────────────────────────────────────
@router.get("/following/{username}")
async def get_following(username: str, request: Request):
    db     = request.app.state.db
    target = await db.users.find_one({"username": username})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    target_id    = str(target["_id"])
    follow_docs  = await db.follows.find({"follower_id": target_id}).to_list(500)
    following_ids = [f["following_id"] for f in follow_docs]
    if not following_ids:
        return []
    users = await db.users.find({"_id": {"$in": following_ids}}, {"password": 0}).to_list(500)
    # Apply lifetime override — same reasoning as in get_followers above.
    db_lifetime_docs = await db.lifetime_accounts.find({}).to_list(500)
    db_lifetime_map = {d["_id"]: d for d in db_lifetime_docs}
    def _row(u):
        uname = u.get("username", "")
        cfg = LIFETIME_ACCOUNTS.get(uname) or (
            {"plan": db_lifetime_map[uname].get("plan", "artist")}
            if uname in db_lifetime_map else None
        )
        is_online = False
        last_seen = u.get("last_seen_at")
        if isinstance(last_seen, datetime):
            is_online = 0 <= (datetime.utcnow() - last_seen).total_seconds() <= 120
        return {
            "username":  uname,
            "name":      u.get("name", ""),
            "avatarUrl": u.get("avatarUrl", ""),
            "plan":      cfg["plan"] if cfg else u.get("plan", "free"),
            "is_online": is_online,
        }
    return [_row(u) for u in users]


# ── Follow / unfollow ─────────────────────────────────────────────
@router.post("/follow/{username}")
async def follow_user(username: str, request: Request, user=Depends(get_current_user)):
    db     = request.app.state.db
    target = await db.users.find_one({"username": username})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    follower_id  = str(user["_id"])
    following_id = str(target["_id"])

    if follower_id == following_id:
        raise HTTPException(status_code=400, detail="Cannot follow yourself")

    # If either user has blocked the other, follow is not allowed
    db_blocks = await db.blocks.find_one({
        "$or": [
            {"blocker_id": follower_id,  "blocked_id": following_id},
            {"blocker_id": following_id, "blocked_id": follower_id},
        ]
    })
    if db_blocks:
        raise HTTPException(status_code=403, detail="Unable to follow this user")

    await db.follows.update_one(
        {"follower_id": follower_id, "following_id": following_id},
        {"$setOnInsert": {
            "follower_id":  follower_id,
            "following_id": following_id,
            "created_at":   datetime.utcnow(),
        }},
        upsert=True,
    )

    # Auto-accept any pending DM thread from the user we just
    # followed. Instagram pattern: following someone implicitly
    # accepts them into your DMs. Without this, you'd follow someone
    # back and their message would still sit in Requests, requiring
    # a second tap. The thread doc lives in db.message_threads and
    # is keyed by alphabetically-sorted pair.
    me_name     = user.get("username", "")
    target_name = target.get("username", "")
    if me_name and target_name:
        thread_id = ":".join(sorted([me_name, target_name]))
        await db.message_threads.update_one(
            {"_id": thread_id},
            {
                "$setOnInsert": {"users": sorted([me_name, target_name])},
                "$addToSet":    {"accepted_by": me_name},
            },
            upsert=True,
        )

    return {"success": True, "following": True}


@router.delete("/follow/{username}")
async def unfollow_user(username: str, request: Request, user=Depends(get_current_user)):
    db     = request.app.state.db
    target = await db.users.find_one({"username": username})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    await db.follows.delete_one({
        "follower_id":  str(user["_id"]),
        "following_id": str(target["_id"]),
    })
    return {"success": True, "following": False}


# ── Block / unblock ───────────────────────────────────────────────
# Blocking is stricter than unfollowing:
#   - The blocked user can no longer see the blocker's profile, posts, beats
#     (filtered server-side wherever feasible) or send them messages
#   - Both directions of any existing follow relationship are removed
#   - The blocked user is NOT notified
# Blocks live in their own `blocks` collection with the same key pattern as
# follows: blocker_id, blocked_id, created_at.
@router.post("/block/{username}")
async def block_user(username: str, request: Request, user=Depends(get_current_user)):
    db     = request.app.state.db
    target = await db.users.find_one({"username": username})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    blocker_id = str(user["_id"])
    blocked_id = str(target["_id"])

    if blocker_id == blocked_id:
        raise HTTPException(status_code=400, detail="Cannot block yourself")

    # Upsert the block record
    await db.blocks.update_one(
        {"blocker_id": blocker_id, "blocked_id": blocked_id},
        {"$setOnInsert": {
            "blocker_id": blocker_id,
            "blocked_id": blocked_id,
            "created_at": datetime.utcnow(),
        }},
        upsert=True,
    )

    # Sever follows in BOTH directions so blocked accounts don't keep
    # showing up in feeds / followers / following counts
    await db.follows.delete_one({"follower_id":  blocker_id, "following_id": blocked_id})
    await db.follows.delete_one({"follower_id":  blocked_id, "following_id": blocker_id})

    return {"success": True, "blocked": True}


@router.delete("/block/{username}")
async def unblock_user(username: str, request: Request, user=Depends(get_current_user)):
    db     = request.app.state.db
    target = await db.users.find_one({"username": username})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    await db.blocks.delete_one({
        "blocker_id": str(user["_id"]),
        "blocked_id": str(target["_id"]),
    })
    return {"success": True, "blocked": False}


@router.get("/blocked-users")
async def list_blocked_users(request: Request, user=Depends(get_current_user)):
    """List all users the current user has blocked — used by the Settings
    'Blocked Users' panel so they can unblock people."""
    db = request.app.state.db
    block_docs = await db.blocks.find({"blocker_id": str(user["_id"])}).to_list(500)
    blocked_ids = [b["blocked_id"] for b in block_docs]
    if not blocked_ids:
        return []
    from bson import ObjectId as _ObjId
    valid_ids = []
    for bid in blocked_ids:
        try: valid_ids.append(_ObjId(bid))
        except Exception: pass
    if not valid_ids:
        return []
    users = await db.users.find(
        {"_id": {"$in": valid_ids}},
        {"username": 1, "name": 1, "avatarUrl": 1, "plan": 1}
    ).to_list(500)
    return [
        {
            "username":  u.get("username", ""),
            "name":      u.get("name", ""),
            "avatarUrl": u.get("avatarUrl", ""),
            "plan":      u.get("plan", "free"),
        }
        for u in users
    ]


@router.get("/block-status/{username}")
async def block_status(username: str, request: Request, user=Depends(get_current_user)):
    """Return whether the current user has blocked `username`, AND whether
    `username` has blocked the current user. The frontend uses this to:
      - Show 'Block / Unblock' button state on profiles
      - Show 'This user has blocked you' empty state if reverse-blocked
    """
    db     = request.app.state.db
    target = await db.users.find_one({"username": username})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    me_id  = str(user["_id"])
    them_id = str(target["_id"])

    i_blocked_them = await db.blocks.find_one({"blocker_id": me_id, "blocked_id": them_id})
    they_blocked_me = await db.blocks.find_one({"blocker_id": them_id, "blocked_id": me_id})

    return {
        "i_blocked_them":  bool(i_blocked_them),
        "they_blocked_me": bool(they_blocked_me),
    }


# ── Activity feed: new uploads + posts from followed users ─────
# Returns a merged, time-sorted stream of activity from people the user
# follows. Two kinds of items:
#   - kind="beat":  a new producer beat upload (Producer Pro accounts)
#   - kind="post":  a status, music, or video post (any account that posts)
# Each item carries a 'created_at' ISO timestamp so the client can sort
# or group as needed. Returns [] when the user follows nobody or has
# no recent activity from their network.
@router.get("/feed")
async def activity_feed(request: Request, limit: int = 30, user=Depends(get_current_user)):
    db = request.app.state.db
    user_id = str(user["_id"])
    limit = max(1, min(int(limit), 100))

    # Get list of user IDs the current user follows
    follow_docs = await db.follows.find({"follower_id": user_id}).to_list(500)
    following_ids = [f["following_id"] for f in follow_docs]

    # ALWAYS include the user themselves so they see their own posts in
    # the feed (Twitter / X behaviour — your own activity is part of your
    # timeline). Even with zero follows, the user still sees their stuff.
    feed_user_ids = list(set(following_ids + [user_id]))

    # Exclude anyone the user has blocked OR who has blocked the user.
    # The block endpoint already unfollows in both directions, but we filter
    # again here defensively in case of stale data. Never exclude the user
    # themselves from their own feed.
    block_docs = await db.blocks.find({
        "$or": [{"blocker_id": user_id}, {"blocked_id": user_id}]
    }).to_list(1000)
    blocked_set = set()
    for b in block_docs:
        blocked_set.add(b.get("blocker_id"))
        blocked_set.add(b.get("blocked_id"))
    blocked_set.discard(user_id)
    feed_user_ids = [fid for fid in feed_user_ids if fid not in blocked_set]
    if not feed_user_ids:
        return []

    # Batch-fetch user details up front so we have avatar/username for
    # every item without per-item queries. Includes the current user too.
    #
    # The users collection contains a mix of legacy ObjectId _ids and newer
    # string _ids (register() now uses str(ObjectId())). We query BOTH formats
    # in one $or so neither group is missed. Without this, the user_map ended
    # up empty for string-_id users, which silently emptied the post username
    # filter below — meaning posts never appeared in the feed.
    from bson import ObjectId as _ObjId
    objid_pids = []
    for pid in feed_user_ids:
        try: objid_pids.append(_ObjId(pid))
        except Exception: pass

    user_map = {}
    # We also key by username — some legacy posts have user_id stored
    # as an ObjectId while user docs have string _id (or vice-versa),
    # which silently broke the _id-based lookup and stripped plan/
    # avatar off the payload. Looking up by username instead is robust
    # to any ID-format mismatch since posts always carry the author's
    # username verbatim.
    user_map_by_name = {}
    or_clauses = [{"_id": {"$in": feed_user_ids}}]  # string _ids
    if objid_pids:
        or_clauses.append({"_id": {"$in": objid_pids}})  # legacy ObjectId _ids

    followed = await db.users.find(
        {"$or": or_clauses},
        {"avatarUrl": 1, "username": 1, "name": 1, "plan": 1}
    ).to_list(500)
    for f in followed:
        info = {
            "username":  f.get("username", ""),
            "name":      f.get("name", ""),
            "avatarUrl": f.get("avatarUrl", ""),
            "plan":      f.get("plan", "free"),
        }
        user_map[str(f["_id"])] = info
        if info["username"]:
            user_map_by_name[info["username"]] = info

    items = []

    # 1) Recent beat uploads from followed producers (and user's own beats)
    # Over-fetch so the final merge-trim doesn't starve the OTHER kind of
    # item (posts). A producer who uploads 30 beats today shouldn't push
    # everyone else's status posts off the feed entirely.
    fetch_each = max(limit, 50)
    try:
        beat_docs = await db.producer_beats.find(
            {"producer_id": {"$in": feed_user_ids}}
        ).sort("uploaded_at", -1).limit(fetch_each).to_list(fetch_each)
        for b in beat_docs:
            pid = b.get("producer_id", "")
            pinfo = user_map.get(pid, {})
            uploaded_at = b.get("uploaded_at")
            items.append({
                "kind":              "beat",
                "id":                str(b["_id"]),
                "created_at":        _iso_utc(uploaded_at),
                "_sort_ts":          uploaded_at.timestamp() if uploaded_at else 0,
                # User
                "username":          pinfo.get("username", b.get("producer_username", "")),
                "user_avatar":       pinfo.get("avatarUrl", b.get("producer_avatar", "")),
                # Beat fields (matching /api/producer/beats shape)
                "title":             b.get("title"),
                "genre":             b.get("genre"),
                "price":             b.get("price", "free"),
                "url":               b.get("url"),
                "producer":          b.get("producer"),
                "producer_id":       pid,
                "producer_username": pinfo.get("username", b.get("producer_username", "")),
                "producer_avatar":   pinfo.get("avatarUrl", b.get("producer_avatar", "")),
                "stripe_account_id": b.get("stripe_account_id"),
                "downloads":         b.get("downloads", 0),
                "playCount":         b.get("playCount", 0),
                "description":       b.get("description", ""),
                "bpm":               b.get("bpm", 0),
                "key":               b.get("key", ""),
                "preview_start":     b.get("preview_start", 0),
                "uploaded_at":       _iso_utc(uploaded_at),
                "basic_lease_price":   b.get("basic_lease_price", 50 if b.get("price", "free") != "free" else 0),
                "premium_lease_price": b.get("premium_lease_price", 0),
                "premium_sold":        bool(b.get("premium_sold", False)),
                "premium_sold_to":     b.get("premium_sold_to"),
            })
    except Exception:
        pass

    # 2) Recent posts (status / music / video) — from followed users AND
    # from the current user themselves. Tries a few common collection
    # names since the posts router may use any of these. Silently skips
    # if none exist or query errors.
    posts_coll = None
    for name in ("posts", "user_posts", "social_posts"):
        try:
            if name in await db.list_collection_names():
                posts_coll = db[name]
                break
        except Exception:
            continue
    if posts_coll is not None:
        try:
            # Posts may store author by user_id (ObjectId or str), or by
            # username. We support all three. The username path is what
            # posts.py actually uses today.
            usernames_in_feed = [u.get("username") for u in user_map.values() if u.get("username")]
            post_query = {
                "$or": [
                    {"user_id":   {"$in": feed_user_ids}},
                    {"author_id": {"$in": feed_user_ids}},
                    {"username":  {"$in": usernames_in_feed}},
                ]
            }
            # Sort by createdAt (camelCase — what posts.py writes). Falls
            # back to created_at for old / alternative-named docs.
            post_docs = await posts_coll.find(post_query).sort([
                ("createdAt", -1), ("created_at", -1),
            ]).limit(fetch_each).to_list(fetch_each)

            # If any of these posts are reposts, batch-fetch their originals
            # so we can inline the original content. Reposts have
            # `repost_of: <original_id>` set.
            originals_map = {}
            original_ids = [p.get("repost_of") for p in post_docs if p.get("repost_of")]
            if original_ids:
                orig_docs = await posts_coll.find(
                    {"_id": {"$in": original_ids}}
                ).to_list(len(original_ids))
                originals_map = {str(o["_id"]): o for o in orig_docs}

                # Top up user_map_by_name with any original-post authors
                # we don't already know about (you can repost someone you
                # don't follow). Without this, the inlined original_post
                # payload would have an empty plan and the frontend
                # wouldn't render the reposted user's verified tick.
                missing_names = set()
                for o in orig_docs:
                    nm = o.get("username", "")
                    if nm and nm not in user_map_by_name:
                        missing_names.add(nm)
                if missing_names:
                    extra_docs = await db.users.find(
                        {"username": {"$in": list(missing_names)}},
                        {"avatarUrl": 1, "username": 1, "name": 1, "plan": 1},
                    ).to_list(length=len(missing_names) + 10)
                    for x in extra_docs:
                        info = {
                            "username":  x.get("username", ""),
                            "name":      x.get("name", ""),
                            "avatarUrl": x.get("avatarUrl", ""),
                            "plan":      x.get("plan", "free"),
                        }
                        user_map[str(x["_id"])] = info
                        if info["username"]:
                            user_map_by_name[info["username"]] = info

            def _post_payload(p, created):
                """Build the per-post payload used by the feed. Inline so it
                shares the user_map / originals_map from this closure."""
                author_id = str(p.get("user_id") or p.get("author_id") or "")
                ainfo = user_map.get(author_id) or {}
                # Fallback by username — covers posts whose user_id is in
                # a different format than the user doc's _id (legacy
                # ObjectId vs new string-_id mismatch). Without this the
                # plan field gets stripped for those authors and the
                # frontend's verified-tick logic silently fails.
                if not ainfo:
                    post_username = p.get("username", "")
                    if post_username:
                        ainfo = user_map_by_name.get(post_username, {}) or {}
                return {
                    "username":     ainfo.get("username", p.get("username", "")),
                    "user_avatar":  ainfo.get("avatarUrl", p.get("avatarUrl", "")),
                    # Author's plan — used by the frontend to decide
                    # whether to render the verified tick next to their
                    # name on post cards. May be empty for legacy posts
                    # whose author can't be resolved in user_map.
                    "plan":         ainfo.get("plan", ""),
                    "text":         p.get("text", ""),
                    "images":       p.get("images", []),
                    "type":         p.get("type", "status"),
                    "embedUrl":     p.get("embedUrl", ""),
                    "videoUrl":     p.get("videoUrl", ""),
                    "caption":      p.get("caption", ""),
                    "likeCount":    p.get("likeCount", 0),
                    "commentCount": p.get("commentCount", 0),
                    "repostCount":  p.get("repostCount", 0),
                    # Link preview fields — empty strings when the post
                    # didn't include a URL, otherwise stored OG metadata.
                    "linkUrl":         p.get("linkUrl", ""),
                    "linkTitle":       p.get("linkTitle", ""),
                    "linkDescription": p.get("linkDescription", ""),
                    "linkImage":       p.get("linkImage", ""),
                    "linkSiteName":    p.get("linkSiteName", ""),
                    "id":           str(p.get("_id")),
                    "created_at":   _iso_utc(p.get("createdAt") or p.get("created_at")),
                }

            for p in post_docs:
                created = p.get("createdAt") or p.get("created_at")
                base = _post_payload(p, created)
                item = {
                    "kind":         "post",
                    "id":           str(p.get("_id")),
                    "created_at":   _iso_utc(created),
                    "_sort_ts":     created.timestamp() if hasattr(created, "timestamp") else 0,
                }
                item.update(base)
                # Repost handling — if this doc is a repost, attach the
                # underlying original as `original_post` so the client can
                # render its content with the reposter's identity above.
                if p.get("repost_of"):
                    item["repost_of"] = p["repost_of"]
                    orig = originals_map.get(p["repost_of"])
                    if orig:
                        # Original's like/comment/repost counts go in too —
                        # the client uses them for the action bar.
                        item["original_post"] = _post_payload(orig, orig.get("createdAt"))
                items.append(item)
        except Exception:
            pass

    # Sort all items newest-first by their timestamp, then strip internal field
    items.sort(key=lambda x: x.get("_sort_ts", 0), reverse=True)
    items = items[:limit]
    for it in items:
        it.pop("_sort_ts", None)
    return items


# ── Change password ───────────────────────────────────────────────
class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password:     str

@router.post("/change-password")
async def change_password(body: ChangePasswordRequest, request: Request, user=Depends(get_current_user)):
    db = request.app.state.db
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters.")

    user_doc = await db.users.find_one({"_id": user["_id"]})
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")

    stored_pw = user_doc.get("password") or user_doc.get("hashed_password", "")
    if not verify_password(body.current_password, stored_pw):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"password": hash_password(body.new_password)}}
    )
    return {"success": True, "message": "Password changed successfully"}


# ── Forgot / reset password ───────────────────────────────────────
import secrets as secrets_mod
import os
import httpx as httpx_mod

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FRONTEND_URL   = os.getenv("FRONTEND_URL", "https://beatfinder.co.uk")
FROM_EMAIL     = os.getenv("FROM_EMAIL", "BeatFinder <support@beatfinder.co.uk>")

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    token:        str
    new_password: str

@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, request: Request):
    db       = request.app.state.db
    # Run one-time email-normalization migration if needed
    await _ensure_email_normalization_ready(db, request.app.state)
    # Try normalized lookup first (Gmail tricks + case insensitive),
    # fall back to legacy lowercased exact match for users not yet
    # backfilled.
    norm = normalize_email(body.email)
    user_doc = None
    if norm:
        user_doc = await db.users.find_one({"normalized_email": norm})
    if not user_doc:
        user_doc = await db.users.find_one({"email": (body.email or "").lower().strip()})
    if not user_doc:
        return {"success": True, "message": "If that email exists you will receive a reset link."}

    token      = secrets_mod.token_urlsafe(32)
    expires_at = datetime.utcnow().timestamp() + 3600
    await db.password_resets.insert_one({
        "token": token, "user_id": str(user_doc["_id"]),
        "email": body.email.lower().strip(), "expires_at": expires_at, "used": False,
    })

    reset_url = FRONTEND_URL + "?reset_token=" + token
    html = f"""
<div style="font-family:sans-serif;max-width:500px;margin:0 auto;background:#0a0a0a;color:white;padding:32px;border-radius:16px">
  <div style="font-size:32px;font-weight:900;letter-spacing:4px;color:#C026D3;margin-bottom:8px">BEATFINDER</div>
  <div style="color:white;font-size:20px;font-weight:700;margin-bottom:12px">Reset Your Password</div>
  <div style="color:#aaa;margin-bottom:24px">Click the button below to reset your password. This link expires in 1 hour.</div>
  <a href='{reset_url}' style="display:block;background:linear-gradient(135deg,#C026D3,#7C3AED);border-radius:12px;color:white;font-weight:800;font-size:16px;padding:16px;text-align:center;text-decoration:none;margin-bottom:24px">Reset My Password</a>
  <div style="color:#555;font-size:12px">If you didn't request this, ignore this email.</div>
</div>"""

    async with httpx_mod.AsyncClient(timeout=10.0) as client:
        await client.post("https://api.resend.com/emails",
            headers={"Authorization": "Bearer " + RESEND_API_KEY, "Content-Type": "application/json"},
            json={"from": FROM_EMAIL, "to": [body.email],
                  "subject": "Reset your BeatFinder password", "html": html})

    return {"success": True, "message": "If that email exists you will receive a reset link."}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest, request: Request):
    db  = request.app.state.db
    doc = await db.password_resets.find_one({"token": body.token, "used": False})
    if not doc:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link.")
    if datetime.utcnow().timestamp() > doc["expires_at"]:
        raise HTTPException(status_code=400, detail="Reset link has expired.")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")

    new_hash = hash_password(body.new_password)
    user_id  = doc["user_id"]

    # User _id storage is mixed across the DB (string for newer accounts,
    # ObjectId for legacy). update_user_by_id handles both formats so we
    # don't silently no-op the password update for half the userbase.
    from db_helpers import update_user_by_id
    result = await update_user_by_id(db, user_id, {"$set": {"password": new_hash}})

    # Fail loudly if we still couldn't find them — better to surface the
    # error than to claim success and lock them out of their account.
    if result.matched_count == 0:
        raise HTTPException(
            status_code=500,
            detail="Reset failed — could not locate your account. Contact support."
        )

    await db.password_resets.update_one({"token": body.token}, {"$set": {"used": True}})
    return {"success": True, "message": "Password reset successfully."}



# ── Upload profile photo ──────────────────────────────────────────
@router.post("/avatar")
async def upload_avatar(
    request: Request,
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    import httpx, hashlib, time as _time

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image (JPEG, PNG, etc.)")

    file_bytes = await file.read()
    if len(file_bytes) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large — maximum 5MB")

    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    api_key    = os.getenv("CLOUDINARY_API_KEY", "")
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "")

    if not cloud_name or not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="Image storage not configured")

    timestamp = int(_time.time())
    folder    = "beatfinder/avatars"
    public_id = "avatar_" + str(user["_id"])

    # Cloudinary signature
    to_sign   = f"folder={folder}&public_id={public_id}&timestamp={timestamp}" + api_secret
    signature = hashlib.sha256(to_sign.encode()).hexdigest()

    upload_url = f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload"

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            upload_url,
            data={
                "api_key":   api_key,
                "timestamp": timestamp,
                "folder":    folder,
                "public_id": public_id,
                "signature": signature,
            },
            files={"file": (file.filename, file_bytes, file.content_type)},
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Image upload failed: " + resp.text)

    avatar_url = resp.json().get("secure_url", "")

    # Save to user document
    db = request.app.state.db
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"avatarUrl": avatar_url}}
    )

    # Sync producer_avatar on all beats by this user so cards show latest photo
    await db.producer_beats.update_many(
        {"producer_id": str(user["_id"])},
        {"$set": {"producer_avatar": avatar_url}}
    )

    return {"avatarUrl": avatar_url}



# ── Upload header photo ───────────────────────────────────────────
@router.post("/header")
async def upload_header(
    request: Request,
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    import httpx, hashlib, time as _time

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    file_bytes = await file.read()
    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large - max 10MB")

    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    api_key    = os.getenv("CLOUDINARY_API_KEY", "")
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "")

    if not cloud_name or not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="Image storage not configured")

    timestamp = int(_time.time())
    folder    = "beatfinder/headers"
    public_id = "header_" + str(user["_id"])

    # Sign only folder, public_id, timestamp (NOT transformation)
    to_sign   = f"folder={folder}&public_id={public_id}&timestamp={timestamp}" + api_secret
    signature = hashlib.sha256(to_sign.encode()).hexdigest()

    upload_url = f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload"

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            upload_url,
            data={
                "api_key":   api_key,
                "timestamp": timestamp,
                "folder":    folder,
                "public_id": public_id,
                "signature": signature,
                "crop":      "fill",
                "gravity":   "center",
                "width":     1200,
                "height":    400,
            },
            files={"file": (file.filename, file_bytes, file.content_type)},
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Header upload failed: " + resp.text)

    header_url = resp.json().get("secure_url", "")

    db = request.app.state.db
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"headerUrl": header_url}}
    )

    return {"headerUrl": header_url}


# ── Upload beat artwork (default image for ALL beats by this producer) ───
# Producers can set one image that displays on every beat card they upload.
# Updating this updates ALL existing beats by this producer so cards show
# the latest artwork. Mirrors the avatar upload pattern.
@router.post("/beat-artwork")
async def upload_beat_artwork(
    request: Request,
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    import httpx, hashlib, time as _time

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image (JPEG, PNG, etc.)")

    file_bytes = await file.read()
    if len(file_bytes) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large — maximum 5MB")

    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    api_key    = os.getenv("CLOUDINARY_API_KEY", "")
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "")

    if not cloud_name or not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="Image storage not configured")

    timestamp = int(_time.time())
    folder    = "beatfinder/beat-artwork"
    public_id = "artwork_" + str(user["_id"])

    to_sign   = f"folder={folder}&public_id={public_id}&timestamp={timestamp}" + api_secret
    signature = hashlib.sha256(to_sign.encode()).hexdigest()

    upload_url = f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload"

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            upload_url,
            data={
                "api_key":   api_key,
                "timestamp": timestamp,
                "folder":    folder,
                "public_id": public_id,
                "signature": signature,
            },
            files={"file": (file.filename, file_bytes, file.content_type)},
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Artwork upload failed: " + resp.text)

    artwork_url = resp.json().get("secure_url", "")

    db = request.app.state.db
    # Save on user record so future uploads inherit it automatically
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"beatArtworkUrl": artwork_url}}
    )
    # Apply retroactively to every beat this producer has uploaded
    await db.producer_beats.update_many(
        {"producer_id": str(user["_id"])},
        {"$set": {"beat_artwork": artwork_url}}
    )

    return {"beatArtworkUrl": artwork_url}


# ── Admin: generate activation codes ─────────────────────────────
class GenerateCodeRequest(BaseModel):
    plan:  str
    count: int = 1

@router.post("/generate-codes")
async def generate_codes(body: GenerateCodeRequest, request: Request, user=Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    if body.plan not in PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan")
    import secrets
    db    = request.app.state.db
    codes = []
    for _ in range(body.count):
        prefix = "ART" if body.plan == "artist" else "PRD"
        code   = prefix + "-" + secrets.token_hex(3).upper()
        await db.activation_codes.insert_one({
            "_id": code, "plan": body.plan, "used": False, "created_at": datetime.utcnow(),
        })
        codes.append(code)
    return {"codes": codes, "plan": body.plan}


# ── Activate plan with code ───────────────────────────────────────
class ActivateRequest(BaseModel):
    code: str

@router.post("/activate")
async def activate_plan(body: ActivateRequest, request: Request, user=Depends(get_current_user)):
    db  = request.app.state.db
    doc = await db.activation_codes.find_one({"_id": body.code.strip().upper()})
    if not doc:
        raise HTTPException(status_code=400, detail="Invalid activation code")
    if doc.get("used"):
        raise HTTPException(status_code=400, detail="This code has already been used")

    plan = doc["plan"]
    await db.activation_codes.update_one(
        {"_id": body.code.strip().upper()},
        {"$set": {"used": True, "used_by": str(user["_id"]), "used_at": datetime.utcnow()}}
    )
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"plan": plan, "upgraded_at": datetime.utcnow()}}
    )
    return {"success": True, "plan": plan, "message": plan + " plan activated successfully!"}


# ── Beat play tracking ────────────────────────────────────────────────────────
# Rules:
#   - Only counts when audio actually plays (called from onPlay after 3s)
#   - Anti-spam: one count per (beat_id + ip_hash) per 30 minutes
#   - Atomically increments beat.playCount and owner.totalPlayCount
#   - Records a BeatPlay document for analytics

import hashlib as _hashlib

@router.post("/beat-play/{beat_id}")
async def record_beat_play(beat_id: str, request: Request):
    db = request.app.state.db

    # Validate beat exists
    try:
        beat = await db.producer_beats.find_one({"_id": ObjectId(beat_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid beat ID")
    if not beat:
        raise HTTPException(status_code=404, detail="Beat not found")

    # Hash the IP — never store raw IP
    raw_ip  = request.client.host if request.client else "unknown"
    ip_hash = _hashlib.sha256(raw_ip.encode()).hexdigest()[:16]

    # Check 30-min cooldown per ip+beat
    from datetime import timedelta
    cutoff   = datetime.utcnow() - timedelta(seconds=1800)
    existing = await db.beat_plays.find_one({
        "beat_id":   beat_id,
        "ip_hash":   ip_hash,
        "played_at": {"$gte": cutoff},
    })
    if existing:
        return {"counted": False, "playCount": beat.get("playCount", 0), "reason": "cooldown"}

    # Atomic increment on beat
    result = await db.producer_beats.find_one_and_update(
        {"_id": ObjectId(beat_id)},
        {"$inc": {"playCount": 1}},
        return_document=True,
    )
    new_count = result.get("playCount", 1) if result else 1

    # Atomic increment on owner totalPlayCount — both id formats
    producer_id = beat.get("producer_id")
    if producer_id:
        from db_helpers import update_user_by_id
        await update_user_by_id(db, producer_id, {"$inc": {"totalPlayCount": 1}})

    # Analytics record
    await db.beat_plays.insert_one({
        "beat_id":    beat_id,
        "ip_hash":    ip_hash,
        "producer_id": producer_id or "",
        "played_at":  datetime.utcnow(),
    })

    return {"counted": True, "playCount": new_count}


@router.get("/beat-play/{beat_id}")
async def get_beat_play_count(beat_id: str, request: Request):
    db = request.app.state.db
    try:
        beat = await db.producer_beats.find_one({"_id": ObjectId(beat_id)}, {"playCount": 1})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid beat ID")
    if not beat:
        raise HTTPException(status_code=404, detail="Beat not found")
    return {"playCount": beat.get("playCount", 0)}


# =============================================================================
# ARTIST TRACKS — upload/list/delete tracks for Artist Pro users
# Tracks are songs artists have recorded, can tag producer @mentions
# =============================================================================

import os as _os
import hashlib as _hashlib
import hmac as _hmac
import time as _time

_CLOUD_NAME  = _os.getenv("CLOUDINARY_CLOUD_NAME", "")
_API_KEY_CLD = _os.getenv("CLOUDINARY_API_KEY", "")
_API_SECRET  = _os.getenv("CLOUDINARY_API_SECRET", "")

async def _upload_track_to_cloudinary(data: bytes, filename: str) -> str:
    """Upload audio to Cloudinary using signed HTTP POST — no SDK needed."""
    import httpx
    public_id  = "tracks/" + filename.replace(" ", "_").rsplit(".", 1)[0]
    timestamp  = str(int(_time.time()))
    params     = f"public_id={public_id}&resource_type=video&timestamp={timestamp}"
    signature  = _hashlib.sha1(
        (params + _API_SECRET).encode()
    ).hexdigest()

    upload_url = f"https://api.cloudinary.com/v1_1/{_CLOUD_NAME}/video/upload"
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(upload_url, data={
            "api_key":      _API_KEY_CLD,
            "timestamp":    timestamp,
            "public_id":    public_id,
            "signature":    signature,
            "resource_type":"video",
        }, files={"file": (filename, data, "audio/mpeg")})
        if resp.status_code != 200:
            raise Exception(f"Cloudinary upload failed: {resp.text}")
        return resp.json().get("secure_url", "")


@router.post("/tracks/upload")
async def upload_track(
    request: Request,
    user=Depends(get_current_user),
    file: UploadFile = File(...),
):
    """Artist Pro users upload their recorded tracks (MP3/WAV)."""
    db   = request.app.state.db
    # Effective plan applies lifetime override so granted-lifetime
    # users can upload tracks.
    plan = await get_effective_plan(db, user)
    if plan not in ("artist","producer"):
        raise HTTPException(status_code=403, detail="Artist Pro plan required to upload tracks")

    allowed = (".mp3",".wav",".m4a",".aac",".ogg")
    if not any(file.filename.lower().endswith(e) for e in allowed):
        raise HTTPException(status_code=400, detail="Only MP3/WAV/M4A audio files supported")

    data = await file.read()
    if len(data) > 80 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Maximum 80MB.")

    url = await _upload_track_to_cloudinary(data, file.filename)
    if not url:
        raise HTTPException(status_code=500, detail="Upload failed")

    db = request.app.state.db
    body = {}
    # Pull JSON metadata from form field if sent
    try:
        form = await request.form()
        import json as _json
        meta = _json.loads(form.get("meta","{}"))
        body = meta
    except Exception:
        pass

    doc = {
        "artist_id":       str(user["_id"]),
        "artist_username": user.get("username",""),
        "artist_avatar":   user.get("avatarUrl",""),
        "artist_name":     user.get("name",""),
        "title":           body.get("title", file.filename.rsplit(".",1)[0]),
        "description":     body.get("description","")[:500],
        "producer_tag":    body.get("producer_tag",""),   # @username of producer
        "beat_title":      body.get("beat_title",""),     # name of beat used
        "url":             url,
        "plays":           0,
        "uploaded_at":     datetime.utcnow(),
    }
    result = await db.artist_tracks.insert_one(doc)
    doc["id"] = str(result.inserted_id)
    return {"success": True, "track": doc}


@router.post("/tracks/{track_id}/update")
async def update_track(track_id: str, request: Request, user=Depends(get_current_user)):
    db   = request.app.state.db
    body = await request.json()
    fields = {}
    if "title"        in body: fields["title"]        = str(body["title"])[:100]
    if "description"  in body: fields["description"]  = str(body["description"])[:500]
    if "producer_tag" in body: fields["producer_tag"] = str(body["producer_tag"])[:50]
    if "beat_title"   in body: fields["beat_title"]   = str(body["beat_title"])[:100]
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update")
    result = await db.artist_tracks.update_one(
        {"_id": ObjectId(track_id), "artist_id": str(user["_id"])},
        {"$set": fields}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Track not found or not yours")
    return {"success": True}


@router.delete("/tracks/{track_id}")
async def delete_track(track_id: str, request: Request, user=Depends(get_current_user)):
    db = request.app.state.db
    result = await db.artist_tracks.delete_one(
        {"_id": ObjectId(track_id), "artist_id": str(user["_id"])}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Track not found or not yours")
    return {"success": True}


@router.get("/tracks/profile/{username}")
async def get_profile_tracks(username: str, request: Request):
    """Public — get all tracks for a profile."""
    db     = request.app.state.db
    tracks = await db.artist_tracks.find(
        {"artist_username": username}
    ).sort("uploaded_at", -1).to_list(50)
    return [
        {
            "id":              str(t["_id"]),
            "title":           t.get("title",""),
            "description":     t.get("description",""),
            "producer_tag":    t.get("producer_tag",""),
            "beat_title":      t.get("beat_title",""),
            "url":             t.get("url",""),
            "plays":           t.get("plays",0),
            "artist_username": t.get("artist_username",""),
            "artist_avatar":   t.get("artist_avatar",""),
            "artist_name":     t.get("artist_name",""),
            "uploaded_at":     t.get("uploaded_at","").isoformat() if t.get("uploaded_at") else "",
        }
        for t in tracks
    ]


@router.get("/tracks/my-tracks")
async def get_my_tracks(request: Request, user=Depends(get_current_user)):
    db     = request.app.state.db
    tracks = await db.artist_tracks.find(
        {"artist_id": str(user["_id"])}
    ).sort("uploaded_at", -1).to_list(50)
    return [
        {
            "id":           str(t["_id"]),
            "title":        t.get("title",""),
            "description":  t.get("description",""),
            "producer_tag": t.get("producer_tag",""),
            "beat_title":   t.get("beat_title",""),
            "url":          t.get("url",""),
            "plays":        t.get("plays",0),
            "uploaded_at":  t.get("uploaded_at","").isoformat() if t.get("uploaded_at") else "",
        }
        for t in tracks
    ]


# ════════════════════════════════════════════════════════════════════════
# ADMIN ENDPOINTS — Lifetime account management
# ════════════════════════════════════════════════════════════════════════
# Only admins (users whose is_admin flag is true) can use these. Admin
# status is determined by:
#   1. Hardcoded LIFETIME_ACCOUNTS dict (Trelli is admin)
#   2. is_admin flag stored on the user doc
#
# Admins can:
#   • Grant lifetime access to any username (artist or producer plan)
#   • Revoke admin-granted lifetime access
#   • View the list of all admin-granted lifetimes
#
# Hardcoded lifetime accounts (Trelli/Mikez/HMbarsdat) CANNOT be revoked
# through this UI — they are permanent. Admin must edit the source code
# to change those.

async def get_admin_user(
    request: Request,
    user=Depends(get_current_user),
):
    """Dependency that requires the requesting user to be an admin.
    Returns the user dict if admin, raises 403 otherwise."""
    db = request.app.state.db
    username = user.get("username", "")
    # Check hardcoded admin status first
    cfg = LIFETIME_ACCOUNTS.get(username)
    is_admin = bool(cfg and cfg.get("is_admin", False))
    # Then check user doc flag (set manually in DB if needed)
    if not is_admin:
        is_admin = bool(user.get("is_admin", False))
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


from pydantic import BaseModel as _BaseModel


class _GrantLifetimeBody(_BaseModel):
    username: str
    plan:     str  # "artist" | "producer"


class _RevokeLifetimeBody(_BaseModel):
    username: str


@router.post("/admin/lifetime/grant")
async def admin_grant_lifetime(
    body: _GrantLifetimeBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    """Grant lifetime access to a username. Stores in db.lifetime_accounts
    so it's picked up by _lifetime_config_async on subsequent /me, /login,
    /search, /profile/* requests."""
    db = request.app.state.db
    uname = (body.username or "").strip()
    plan  = (body.plan or "").strip().lower()
    if not uname:
        raise HTTPException(status_code=400, detail="username is required")
    if plan not in ("artist", "producer"):
        raise HTTPException(status_code=400, detail="plan must be 'artist' or 'producer'")
    # Sanity-check: the username should actually exist as a registered user.
    # We don't enforce this strictly (admin might want to pre-grant a
    # lifetime before the user signs up), but warn in the response.
    existing_user = await db.users.find_one({"username": uname})
    # Don't allow re-granting one of the hardcoded permanent lifetimes —
    # they're already in effect via LIFETIME_ACCOUNTS, and granting them
    # in DB would just be noise.
    if uname in LIFETIME_ACCOUNTS:
        raise HTTPException(
            status_code=400,
            detail=f"{uname} is already a permanent lifetime account (hardcoded)."
        )
    # Upsert — admins can also use this endpoint to change a user's
    # lifetime plan (e.g. artist → producer).
    await db.lifetime_accounts.update_one(
        {"_id": uname},
        {"$set": {
            "plan":       plan,
            "is_admin":   False,
            "granted_at": datetime.utcnow(),
            "granted_by": admin.get("username", ""),
        }},
        upsert=True,
    )
    return {
        "ok":             True,
        "username":       uname,
        "plan":           plan,
        "user_exists":    bool(existing_user),
    }


@router.post("/admin/lifetime/revoke")
async def admin_revoke_lifetime(
    body: _RevokeLifetimeBody,
    request: Request,
    admin=Depends(get_admin_user),
):
    """Revoke an admin-granted lifetime access. Cannot revoke the
    hardcoded permanent lifetimes (Trelli/Mikez/HMbarsdat)."""
    db = request.app.state.db
    uname = (body.username or "").strip()
    if not uname:
        raise HTTPException(status_code=400, detail="username is required")
    if uname in LIFETIME_ACCOUNTS:
        raise HTTPException(
            status_code=400,
            detail=f"{uname} is a permanent lifetime account and cannot be revoked from the admin UI."
        )
    result = await db.lifetime_accounts.delete_one({"_id": uname})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail=f"No lifetime grant found for {uname}")
    return {"ok": True, "username": uname}


@router.get("/admin/lifetime/list")
async def admin_list_lifetimes(
    request: Request,
    admin=Depends(get_admin_user),
):
    """Return all admin-granted lifetime accounts (the editable ones).
    Hardcoded permanent lifetimes are returned separately so the UI
    can show them as read-only."""
    db = request.app.state.db
    docs = await db.lifetime_accounts.find({}).to_list(500)
    granted = [
        {
            "username":   d.get("_id", ""),
            "plan":       d.get("plan", "artist"),
            "granted_at": d.get("granted_at", "").isoformat() if d.get("granted_at") else "",
            "granted_by": d.get("granted_by", ""),
        }
        for d in docs
    ]
    permanent = [
        {"username": uname, "plan": cfg["plan"], "is_admin": cfg.get("is_admin", False)}
        for uname, cfg in LIFETIME_ACCOUNTS.items()
    ]
    return {"granted": granted, "permanent": permanent}


@router.get("/admin/users")
async def admin_list_users(
    request: Request,
    admin=Depends(get_admin_user),
    skip: int = 0,
    limit: int = 20,
    q: str = "",
    include_deleted: bool = False,
    filter_by: str = "",
):
    """Paginated list of all users for the admin dashboard.

    Query params:
      • skip            — pagination offset (default 0)
      • limit           — page size, capped at 100 (default 20)
      • q               — case-insensitive search across username/email/name
      • include_deleted — if true, include soft-deleted accounts (default false)
      • filter_by       — narrow further. One of: "" (all), "active",
                          "lifetime", "free", "admin", "payment_failing".
                          "deleted" only matters when include_deleted=true.

    Returns total count + a page of user summaries. Sensitive fields
    (password, password_resets) are stripped. Lifetime accounts have
    their plan label corrected on the way out so the UI shows the
    effective plan, not the stored one.
    """
    db = request.app.state.db

    # Cap limit to avoid heavy queries
    limit = max(1, min(int(limit or 20), 100))
    skip  = max(0, int(skip or 0))

    # Build filter — empty q = all users
    and_clauses = []

    # Search
    qstr = (q or "").strip()
    if qstr:
        pattern = {"$regex": qstr, "$options": "i"}
        and_clauses.append({"$or": [
            {"username": pattern},
            {"email":    pattern},
            {"name":     pattern},
        ]})

    # Hide deleted by default
    if not include_deleted:
        and_clauses.append({"$or": [
            {"deleted": {"$exists": False}},
            {"deleted": False},
        ]})

    # Filter chips
    fb = (filter_by or "").strip().lower()
    if fb == "active":
        and_clauses.append({"subscriptionActive": True})
    elif fb == "free":
        # No active subscription AND not a hardcoded lifetime account
        and_clauses.append({"$or": [
            {"subscriptionActive": {"$exists": False}},
            {"subscriptionActive": False},
        ]})
    elif fb == "admin":
        and_clauses.append({"is_admin": True})
    elif fb == "payment_failing":
        and_clauses.append({"payment_failing": True})
    elif fb == "online":
        # Active in the last 2 minutes (matches the OnlineDot window)
        from datetime import timedelta
        threshold = datetime.utcnow() - timedelta(seconds=120)
        and_clauses.append({"last_seen_at": {"$gte": threshold}})
    elif fb == "deleted":
        # Show ONLY deleted (overrides include_deleted gate)
        and_clauses = [c for c in and_clauses if "deleted" not in str(c)]
        and_clauses.append({"deleted": True})
    # "lifetime" filter handled post-fetch since it requires checking
    # the LIFETIME_ACCOUNTS hardcoded list + db.lifetime_accounts.

    mongo_filter = {"$and": and_clauses} if and_clauses else {}

    # For "lifetime" filter we have to fetch eligible usernames from both
    # the hardcoded dict AND db.lifetime_accounts, then constrain by them.
    if fb == "lifetime":
        db_lifetime_usernames = [d["_id"] for d in await db.lifetime_accounts.find({}, {"_id": 1}).to_list(500)]
        hardcoded = list(LIFETIME_ACCOUNTS.keys())
        all_lifetime = list(set(db_lifetime_usernames + hardcoded))
        if all_lifetime:
            and_clauses.append({"username": {"$in": all_lifetime}})
            mongo_filter = {"$and": and_clauses}
        else:
            # Empty result if no lifetime accounts exist at all
            return {"total": 0, "skip": skip, "limit": limit, "users": []}

    total = await db.users.count_documents(mongo_filter)

    docs = await db.users.find(
        mongo_filter,
        # Project away the password hash + anything else sensitive.
        # _id is included by default.
        {"password": 0, "password_resets": 0},
    ).sort("created_at", -1).skip(skip).limit(limit).to_list(limit)

    # Batch-fetch DB-granted lifetime accounts so we can flag them
    db_lifetime_docs = await db.lifetime_accounts.find({}).to_list(500)
    db_lifetime_map  = {d["_id"]: d for d in db_lifetime_docs}

    def _row(u):
        uname = u.get("username", "")
        # Compute effective plan (lifetime overrides stored plan)
        cfg = LIFETIME_ACCOUNTS.get(uname) or (
            {"plan": db_lifetime_map[uname].get("plan", "artist"),
             "is_admin": bool(db_lifetime_map[uname].get("is_admin", False))}
            if uname in db_lifetime_map else None
        )
        effective_plan = cfg["plan"] if cfg else u.get("plan", "free")
        is_lifetime    = cfg is not None
        created_at     = u.get("created_at")
        created_iso    = created_at.isoformat() if hasattr(created_at, "isoformat") else (str(created_at) if created_at else "")
        return {
            "id":                  str(u.get("_id", "")),
            "username":            uname,
            "name":                u.get("name", ""),
            "email":               u.get("email", ""),
            "avatarUrl":           u.get("avatarUrl", ""),
            "plan":                effective_plan,
            "is_lifetime":         is_lifetime,
            "is_admin":            bool(u.get("is_admin", False)) or bool(cfg and cfg.get("is_admin", False)),
            "subscription_active": bool(u.get("subscriptionActive", False)) or is_lifetime,
            "billing_interval":    "lifetime" if is_lifetime else u.get("billing_interval", ""),
            "payment_failing":     bool(u.get("payment_failing", False)),
            "deleted":             bool(u.get("deleted", False)),
            "created_at":          created_iso,
            "last_seen_at":        _iso_utc(u.get("last_seen_at")) if u.get("last_seen_at") else "",
            "is_online":           (lambda ls: isinstance(ls, datetime) and 0 <= (datetime.utcnow() - ls).total_seconds() <= 120)(u.get("last_seen_at")),
            "stripe_customer_id":  u.get("stripe_customer_id", "") or "",
        }

    return {
        "total":  total,
        "skip":   skip,
        "limit":  limit,
        "users":  [_row(u) for u in docs],
    }


@router.post("/admin/users/{user_id}/delete")
async def admin_delete_user(
    user_id: str,
    request: Request,
    admin=Depends(get_admin_user),
):
    """Soft-delete a user account as an admin action.

    Mirrors /delete-my-account: anonymises the user row (clears PII,
    sets `deleted: True`), hides their beats, removes lyrics/agreements,
    keeps lease records for legal retention. Records who performed the
    deletion + when, so we have an audit trail.

    Safety rails:
      • Cannot delete yourself (force you to use the self-delete flow,
        which has email-confirm protection)
      • Cannot delete the hardcoded LIFETIME_ACCOUNTS (Trelli/Mikez/
        HMbarsdat) — those are protected. Use revoke-lifetime first
        if you need to remove a permanent admin.
      • Already-deleted users return success (idempotent — re-deleting
        is a no-op, but we don't error)
    """
    from db_helpers import find_user_by_id, update_user_by_id

    target = await find_user_by_id(request.app.state.db, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Don't allow deleting yourself via the admin path
    if str(target.get("_id", "")) == str(admin.get("_id", "")):
        raise HTTPException(
            status_code=400,
            detail="You can't delete yourself from the admin panel. Use Profile → Settings → Delete Account.",
        )

    # Don't allow deleting hardcoded permanent accounts
    target_username = (target.get("username") or "").strip()
    if target_username in LIFETIME_ACCOUNTS:
        raise HTTPException(
            status_code=400,
            detail=f"@{target_username} is a permanent lifetime account and can't be deleted from here.",
        )

    # Idempotent: if already deleted, just return success
    if target.get("deleted"):
        return {
            "success": True,
            "already_deleted": True,
            "message": "Account was already deleted.",
        }

    db   = request.app.state.db
    uid  = target["_id"]
    uid_str = str(uid)
    now  = datetime.utcnow()
    anon_email    = f"deleted-{uid_str}@deleted.beatfinder.invalid"
    anon_username = f"deleted_user_{uid_str[:8]}"

    await update_user_by_id(db, uid, {
        "$set": {
            "deleted":               True,
            "deleted_at":            now,
            "deletion_reason":       f"admin-removed by @{admin.get('username','admin')}",
            "deleted_by":            admin.get("username", ""),
            "email":                 anon_email,
            "username":              anon_username,
            "name":                  "Deleted user",
            "bio":                   "",
            "location":              "",
            "instagram":             "",
            "tiktok":                "",
            "youtube":               "",
            "spotify":               "",
            "website":               "",
            "avatarUrl":             "",
            "headerUrl":             "",
            "appleMusic":            "",
            "stripe_customer_id":    "",
            "subscription_expires_at": None,
            "plan":                  "free",
            "subscriptionActive":    False,
        },
        "$unset": {
            "password":        "",
            "hashed_password": "",
        },
    })

    # Hide their uploaded beats from public visibility
    if "beats" in await db.list_collection_names():
        await db.beats.update_many(
            {"producer_id": uid_str},
            {"$set": {"deleted": True, "deleted_at": now, "hidden": True}},
        )
    if "producer_beats" in await db.list_collection_names():
        await db.producer_beats.update_many(
            {"producer_id": uid_str},
            {"$set": {"deleted": True, "deleted_at": now, "hidden": True}},
        )

    # Soft-delete personal items
    if "lyrics" in await db.list_collection_names():
        await db.lyrics.delete_many({"user_id": uid_str})
    if "free_licence_agreements" in await db.list_collection_names():
        await db.free_licence_agreements.delete_many({"user_id": uid_str})

    # If they were granted lifetime via the admin panel, remove it
    if "lifetime_accounts" in await db.list_collection_names():
        await db.lifetime_accounts.delete_one({"_id": target_username})

    return {
        "success": True,
        "deleted_at": now.isoformat(),
        "anon_username": anon_username,
    }
