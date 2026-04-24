"""
Pydantic models — request bodies and response shapes.
"""

from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime


# ── Auth ──────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    name:     str       = Field(..., min_length=1, max_length=80)
    email:    EmailStr
    password: str       = Field(..., min_length=6)

class LoginRequest(BaseModel):
    email:    EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    user:         dict


# ── Saved Beats ───────────────────────────────────────────────────

class Beat(BaseModel):
    video_id:  str
    title:     str
    channel:   str
    thumbnail: str

class SaveBeatRequest(BaseModel):
    beat: Beat


# ── Subscription Plans ────────────────────────────────────────────

class PlanUpgradeRequest(BaseModel):
    plan: str   # "artist" | "producer"

class PlanResponse(BaseModel):
    plan:         str
    paypal_email: str = "trellitheproducer@gmail.com"
    price_gbp:    float
    paypal_link:  str


# ── YouTube proxy ─────────────────────────────────────────────────

class YouTubeSearchRequest(BaseModel):
    artist_name: str
    max_results: int = Field(default=20, ge=1, le=50)
