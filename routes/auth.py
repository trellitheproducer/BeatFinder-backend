"""
Auth routes: /api/auth/register  /api/auth/login  /api/auth/me
"""

from fastapi import APIRouter, HTTPException, Request, Depends
from bson import ObjectId
from datetime import datetime

from models import RegisterRequest, LoginRequest, TokenResponse, PlanUpgradeRequest, PlanResponse
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
    """Strip internal fields before returning to client."""
    return {
        "id":           str(user["_id"]),
        "name":         user["name"],
        "email":        user["email"],
        "plan":         user.get("plan", "free"),
        "is_admin":     user.get("is_admin", False),
        "created_at":   user.get("created_at", "").isoformat() if user.get("created_at") else None,
    }


# ââ Register ââââââââââââââââââââââââââââââââââââââââââââââââââââââ
@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, request: Request):
    db = request.app.state.db

    if await db.users.find_one({"email": body.email}):
        raise HTTPException(status_code=409, detail="Email already registered")

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


# ââ Login âââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request):
    db   = request.app.state.db
    user = await db.users.find_one({"email": body.email})

    if not user or not verify_password(body.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_token(str(user["_id"]), user["email"])
    return {"access_token": token, "user": _public(user)}


# ââ Me (protected) ââââââââââââââââââââââââââââââââââââââââââââââââ
@router.get("/me")
async def me(user=Depends(get_current_user)):
    return _public(user)


# ââ Upgrade plan (marks paid, frontend verifies PayPal separately) â
@router.post("/upgrade")
async def upgrade_plan(
    body: PlanUpgradeRequest,
    request: Request,
    user=Depends(get_current_user),
):
    if body.plan not in PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan. Choose 'artist' or 'producer'")

    db = request.app.state.db
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"plan": body.plan, "upgraded_at": datetime.utcnow()}},
    )

    plan_info = PLANS[body.plan]
    return {
        "plan":         body.plan,
        "paypal_email": "trellitheproducer@gmail.com",
        "price_gbp":    plan_info["price_gbp"],
        "paypal_link":  plan_info["paypal_link"],
        "message":      f"Pay via PayPal then your {body.plan} plan activates.",
    }
