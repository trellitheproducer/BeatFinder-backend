"""
Auth routes: /api/auth/register  /api/auth/login  /api/auth/me
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

# Pre-generated activation codes - add more as needed
# Format: CODE -> plan type
# Generate new ones with: import secrets; print("BF-" + secrets.token_hex(4).upper())
VALID_CODES = {
    "ARTIST-FREE-TEST":   "artist",
    "PRODUCER-FREE-TEST": "producer",
}


def _public(user: dict) -> dict:
    """Strip internal fields before returning to client."""
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

    if await db.users.find_one({"email": body.email}):
        raise HTTPException(status_code=409, detail="Email already registered")

    # Check username uniqueness (case-insensitive)
    username = body.name.strip()
    if await db.users.find_one({"name": {"$regex": "^" + username + "$", "$options": "i"}}):
        raise HTTPException(status_code=409, detail="That username is already taken. Please choose a different one.")

    user_id = str(ObjectId())
    user = {
        "_id":        user_id,
        "name":       body.name,
        "email":      body.email,
        "password":   hash_password(body.password),
        "plan":       "free",       # free | artist | producer
        "is_admin":   False,
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


# ── Me (protected) ────────────────────────────────────────────────
@router.get("/me")
async def me(user=Depends(get_current_user)):
    return _public(user)


# ── Get PayPal payment link ───────────────────────────────────────────────────
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


# ── Update username ───────────────────────────────────────────────────────────
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

    # Check if username taken by someone else
    existing = await db.users.find_one({"username": username})
    if existing and str(existing["_id"]) != str(user["_id"]):
        raise HTTPException(status_code=409, detail="Username already taken")

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"username": username}}
    )
    return {"success": True, "username": username}


# ── Get public profile by username ────────────────────────────────────────────
@router.get("/profile/{username}")
async def get_public_profile(username: str, request: Request):
    db   = request.app.state.db
    user = await db.users.find_one({"username": username})
    if not user:
        raise HTTPException(status_code=404, detail="Profile not found")

    # Only return public info
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


# ── Activate plan with code ────────────────────────────────────────────────────
class ActivateRequest(BaseModel):
    code: str

@router.post("/activate")
async def activate_plan(
    body: ActivateRequest,
    request: Request,
    user=Depends(get_current_user),
):
    code = body.code.strip().upper()

    # Check if code exists and hasn't been used
    db  = request.app.state.db
    doc = await db.activation_codes.find_one({"_id": code})

    if not doc:
        raise HTTPException(status_code=400, detail="Invalid activation code")
    if doc.get("used"):
        raise HTTPException(status_code=400, detail="This code has already been used")

    plan = doc["plan"]

    # Mark code as used
    await db.activation_codes.update_one(
        {"_id": code},
        {"$set": {"used": True, "used_by": str(user["_id"]), "used_at": datetime.utcnow()}},
    )

    # Upgrade the user
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"plan": plan, "upgraded_at": datetime.utcnow()}},
    )

    return {"success": True, "plan": plan, "message": plan + " plan activated successfully!"}


# ── Admin: generate activation code ───────────────────────────────────────────
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
