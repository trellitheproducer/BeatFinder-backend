"""
Auth routes: /api/auth/
"""

from fastapi import APIRouter, HTTPException, Request, Depends
from bson import ObjectId
from datetime import datetime

from models import RegisterRequest, LoginRequest, TokenResponse, PlanUpgradeRequest, PlanResponse
from pydantic import BaseModel
from auth import hash_password, verify_password, create_token, get_current_user

router = APIRouter()

PLANS = {
    "artist": {
        "price_gbp":   4.99,
        "paypal_link": "https://www.paypal.com/paypalme/trellitheproducer/4.99GBP",
    },
    "producer": {
        "price_gbp":   8.99,
        "paypal_link": "https://www.paypal.com/paypalme/trellitheproducer/8.99GBP",
    },
}


def _public(user: dict) -> dict:
    return {
        "id":           str(user["_id"]),
        "name":         user["name"],
        "email":        user["email"],
        "plan":         user.get("plan", "free"),
        "username":     user.get("username", ""),
        "is_admin":     user.get("is_admin", False),
        "created_at":   user.get("created_at", "").isoformat() if user.get("created_at") else None,
    }


# ── Register ──────────────────────────────────────────────────────
@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, request: Request):
    db = request.app.state.db

    if len(body.password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Password too long. Maximum 72 characters.")

    email = body.email.lower().strip()

    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=409, detail="Email already registered")

    username = body.name.strip()
    if await db.users.find_one({"name": {"$regex": "^" + username + "$", "$options": "i"}}):
        raise HTTPException(status_code=409, detail="That username is already taken. Please choose a different one.")

    user_id = str(ObjectId())
    user = {
        "_id":        user_id,
        "name":       body.name,
        "email":      email,
        "password":   hash_password(body.password),
        "plan":       "free",
        "is_admin":   False,
        "created_at": datetime.utcnow(),
    }
    await db.users.insert_one(user)

    token = create_token(user_id, email)
    return {"access_token": token, "user": _public(user)}


# ── Login ─────────────────────────────────────────────────────────
@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request):
    db   = request.app.state.db
    email = body.email.lower().strip()
    user = await db.users.find_one({"email": email})

    if not user or not verify_password(body.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_token(str(user["_id"]), email)
    return {"access_token": token, "user": _public(user)}


# ── Me ────────────────────────────────────────────────────────────
@router.get("/me")
async def me(user=Depends(get_current_user)):
    return _public(user)


# ── Upgrade ───────────────────────────────────────────────────────
@router.post("/upgrade")
async def upgrade_plan(
    body: PlanUpgradeRequest,
    request: Request,
    user=Depends(get_current_user),
):
    if body.plan not in PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan. Choose artist or producer")
    plan_info = PLANS[body.plan]
    return {
        "plan":         body.plan,
        "paypal_email": "trellitheproducer@gmail.com",
        "price_gbp":    plan_info["price_gbp"],
        "paypal_link":  plan_info["paypal_link"],
        "message":      "Pay via PayPal then use your activation code to unlock.",
    }


# ── Set username ──────────────────────────────────────────────────
class UsernameRequest(BaseModel):
    username: str

@router.post("/username")
async def set_username(
    body: UsernameRequest,
    request: Request,
    user=Depends(get_current_user),
):
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username cannot be empty")
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
    if len(username) > 30:
        raise HTTPException(status_code=400, detail="Username must be under 30 characters")

    import re
    if not re.match(r"^[a-zA-Z0-9_. ]+$", username):
        raise HTTPException(status_code=400, detail="Username can only contain letters, numbers, spaces, dots and underscores")

    db = request.app.state.db
    existing = await db.users.find_one({"username": username})
    if existing and str(existing["_id"]) != str(user["_id"]):
        raise HTTPException(status_code=409, detail="Username already taken")

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"username": username}}
    )
    return {"success": True, "username": username}


# ── Public profile ────────────────────────────────────────────────
@router.get("/profile/{username}")
async def get_public_profile(username: str, request: Request):
    db   = request.app.state.db
    user = await db.users.find_one({"username": username})
    if not user:
        raise HTTPException(status_code=404, detail="Profile not found")

    beats = await db.producer_beats.find(
        {"producer_id": str(user["_id"])}
    ).sort("uploaded_at", -1).to_list(50)

    return {
        "username": user.get("username"),
        "name":     user.get("name"),
        "plan":     user.get("plan", "free"),
        "joined":   user.get("created_at", "").isoformat() if user.get("created_at") else "",
        "beats":    [
            {
                "id":        str(b["_id"]),
                "title":     b.get("title"),
                "genre":     b.get("genre"),
                "price":     b.get("price", "free"),
                "url":       b.get("url"),
                "downloads": b.get("downloads", 0),
            }
            for b in beats
        ],
    }


# ── Activate plan ─────────────────────────────────────────────────
class ActivateRequest(BaseModel):
    code: str

@router.post("/activate")
async def activate_plan(
    body: ActivateRequest,
    request: Request,
    user=Depends(get_current_user),
):
    code = body.code.strip().upper()
    db   = request.app.state.db
    doc  = await db.activation_codes.find_one({"_id": code})

    if not doc:
        raise HTTPException(status_code=400, detail="Invalid activation code")
    if doc.get("used"):
        raise HTTPException(status_code=400, detail="This code has already been used")

    plan = doc["plan"]
    await db.activation_codes.update_one(
        {"_id": code},
        {"$set": {"used": True, "used_by": str(user["_id"]), "used_at": datetime.utcnow()}},
    )
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"plan": plan, "upgraded_at": datetime.utcnow()}},
    )
    return {"success": True, "plan": plan, "message": plan + " plan activated successfully!"}


# ── Forgot password ───────────────────────────────────────────────
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
    email    = body.email.lower().strip()
    user_doc = await db.users.find_one({"email": email})

    # Always return success to prevent email enumeration
    if not user_doc:
        return {"success": True, "message": "If that email exists you will receive a reset link."}

    token      = secrets_mod.token_urlsafe(32)
    expires_at = datetime.utcnow().timestamp() + 3600  # 1 hour

    await db.password_resets.insert_one({
        "token":      token,
        "user_id":    str(user_doc["_id"]),
        "email":      email,
        "expires_at": expires_at,
        "used":       False,
    })

    reset_url = FRONTEND_URL + "?reset_token=" + token

    html = """
<div style="font-family:sans-serif;max-width:500px;margin:0 auto;background:#0a0a0a;color:white;padding:32px;border-radius:16px">
  <div style="font-size:32px;font-weight:900;letter-spacing:4px;color:#C026D3;margin-bottom:8px">BEATFINDER</div>
  <div style="color:#888;margin-bottom:24px">The World's #1 Beat Finder App</div>
  <div style="color:white;font-size:20px;font-weight:700;margin-bottom:12px">Reset Your Password</div>
  <div style="color:#aaa;margin-bottom:24px;line-height:1.7">We received a request to reset your password. Click the button below to create a new one. This link expires in 1 hour.</div>
  <a href='""" + reset_url + """' style="display:block;background:linear-gradient(135deg,#C026D3,#7C3AED);border-radius:12px;color:white;font-weight:800;font-size:16px;padding:16px;text-align:center;text-decoration:none;margin-bottom:24px">Reset My Password</a>
  <div style="color:#555;font-size:12px">If you didn't request this, ignore this email. Your password won't change.</div>
</div>
"""

    async with httpx_mod.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": "Bearer " + RESEND_API_KEY, "Content-Type": "application/json"},
            json={
                "from":    "BeatFinder <onboarding@resend.dev>",
                "to":      [email],
                "subject": "Reset your BeatFinder password",
                "html":    html,
            },
        )
        print("[ForgotPassword] Resend status: " + str(r.status_code) + " | " + r.text[:200])

    return {"success": True, "message": "If that email exists you will receive a reset link."}


# ── Reset password ────────────────────────────────────────────────
@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest, request: Request):
    db  = request.app.state.db
    doc = await db.password_resets.find_one({"token": body.token, "used": False})

    if not doc:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link.")

    if datetime.utcnow().timestamp() > doc["expires_at"]:
        raise HTTPException(status_code=400, detail="Reset link has expired. Please request a new one.")

    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")

    new_hash = hash_password(body.new_password)

    # FIX: use "password" field (same as register), not "hashed_password"
    await db.users.update_one(
        {"_id": doc["user_id"]},
        {"$set": {"password": new_hash}}
    )
    await db.password_resets.update_one(
        {"token": body.token},
        {"$set": {"used": True}}
    )
    return {"success": True, "message": "Password reset successfully. You can now log in."}


# ── Change password ───────────────────────────────────────────────
class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password:     str

@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user=Depends(get_current_user),
):
    db = request.app.state.db

    if len(body.new_password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="New password too long. Maximum 72 characters.")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters.")

    user_doc = await db.users.find_one({"_id": user["_id"]})
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")

    # FIX: use "password" field (same as register/login)
    if not verify_password(body.current_password, user_doc["password"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    new_hash = hash_password(body.new_password)
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"password": new_hash}}
    )
    return {"success": True, "message": "Password changed successfully"}


# ── Generate activation codes (admin) ────────────────────────────
class GenerateCodeRequest(BaseModel):
    plan:  str
    count: int = 1

@router.post("/generate-codes")
async def generate_codes(
    body: GenerateCodeRequest,
    request: Request,
    user=Depends(get_current_user),
):
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
            "_id":        code,
            "plan":       body.plan,
            "used":       False,
            "created_at": datetime.utcnow(),
        })
        codes.append(code)

    return {"codes": codes, "plan": body.plan}
