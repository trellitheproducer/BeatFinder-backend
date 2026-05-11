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

PLANS = {
    "artist":   {"price_gbp": 4.99, "paypal_link": "https://www.paypal.com/paypalme/trellitheproducer/4.99GBP"},
    "producer": {"price_gbp": 8.99, "paypal_link": "https://www.paypal.com/paypalme/trellitheproducer/8.99GBP"},
}


def _public(user: dict) -> dict:
    from datetime import timezone
    expires_at = user.get("subscription_expires_at")
    # Active if expires_at is in the future (or not set for legacy free accounts)
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
    # Free plan users are never "active" via subscription
    if plan == "free":
        sub_active = False
    return {
        "id":                    str(user["_id"]),
        "name":                  user.get("name", ""),
        "email":                 user.get("email", ""),
        "plan":                  plan,
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
    }


# ── Register ──────────────────────────────────────────────────────
@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, request: Request):
    db = request.app.state.db

    if len(body.password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Password too long. Maximum 72 characters.")
    if await db.users.find_one({"email": body.email}):
        raise HTTPException(status_code=409, detail="Email already registered")

    user_id = str(ObjectId())
    user = {
        "_id":        user_id,
        "name":       body.name,
        "email":      body.email,
        "password":   hash_password(body.password),
        "plan":       "free",
        "is_admin":   False,
        "bio":        "",
        "location":   "",
        "created_at": datetime.utcnow(),
    }
    await db.users.insert_one(user)
    token = create_token(user_id, body.email)
    return {"access_token": token, "user": _public(user)}


# ── Login ─────────────────────────────────────────────────────────
@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request):
    db   = request.app.state.db
    user = await db.users.find_one({"email": body.email})
    if not user or not verify_password(body.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_token(str(user["_id"]), user["email"])
    return {"access_token": token, "user": _public(user)}


# ── Me ────────────────────────────────────────────────────────────
@router.get("/me")
async def me(user=Depends(get_current_user)):
    return _public(user)


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
    existing = await db.users.find_one({"username": username})
    if existing and str(existing["_id"]) != str(user["_id"]):
        raise HTTPException(status_code=409, detail="Username already taken")

    await db.users.update_one({"_id": user["_id"]}, {"$set": {"username": username}})
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

    return [
        {
            "username":  d.get("username", ""),
            "name":      d.get("name", ""),
            "plan":      d.get("plan", "free"),
            "bio":       d.get("bio", ""),
            "avatarUrl": d.get("avatarUrl", ""),
        }
        for d in docs if d.get("username")
    ]


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

    play_count = sum(b.get("playCount", 0) for b in beats)

    return {
        "username":       user.get("username"),
        "name":           user.get("name"),
        "plan":           user.get("plan", "free"),
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
        "isFollowing":    False,
        "beats": [
            {
                "id":        str(b["_id"]),
                "title":     b.get("title"),
                "genre":     b.get("genre"),
                "price":     b.get("price", "free"),
                "url":       b.get("url"),
                "downloads": b.get("downloads", 0),
                "playCount": b.get("playCount", 0),
            }
            for b in beats
        ],
    }


# ── Subscription status (called after Stripe redirect + on app load) ─
@router.get("/subscription-status")
async def subscription_status(request: Request, user=Depends(get_current_user)):
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

    play_count = sum(b.get("playCount", 0) for b in beats)

    expires_at = user.get("subscription_expires_at")
    sub_active = False
    if expires_at and user.get("plan", "free") != "free":
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
        "plan":                  user.get("plan", "free"),
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
        "subscriptionActive":    sub_active,
        "subscriptionExpiresAt": expires_at.isoformat() if isinstance(expires_at, datetime) else (str(expires_at) if expires_at else None),
        "billingInterval":       user.get("billing_interval", "monthly"),
        "isFollowing":           is_following,
        "beats": [
            {
                "id":        str(b["_id"]),
                "title":     b.get("title"),
                "genre":     b.get("genre"),
                "price":     b.get("price", "free"),
                "url":       b.get("url"),
                "downloads": b.get("downloads", 0),
                "playCount": b.get("playCount", 0),
            }
            for b in beats
        ],
    }


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

    await db.follows.update_one(
        {"follower_id": follower_id, "following_id": following_id},
        {"$setOnInsert": {
            "follower_id":  follower_id,
            "following_id": following_id,
            "created_at":   datetime.utcnow(),
        }},
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
FRONTEND_URL   = os.getenv("FRONTEND_URL", "https://beat-finder-frontend.vercel.app")

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    token:        str
    new_password: str

@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, request: Request):
    db       = request.app.state.db
    user_doc = await db.users.find_one({"email": body.email.lower().strip()})
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
            json={"from": "BeatFinder <onboarding@resend.dev>", "to": [body.email],
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
    await db.users.update_one(
        {"_id": ObjectId(doc["user_id"])},
        {"$set": {"password": new_hash}}
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
