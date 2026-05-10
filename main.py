"""
BeatFinder API — main.py
Register all routers here.

Add these lines to your existing main.py (or replace it entirely):

    from routes import auth, beats, youtube, producer, stripe_payments, admin, lyrics, messages
    
    app.include_router(auth.router,             prefix="/api/auth")
    app.include_router(beats.router,            prefix="/api/beats")
    app.include_router(youtube.router,          prefix="/api/youtube")
    app.include_router(producer.router,         prefix="/api/producer")
    app.include_router(stripe_payments.router,  prefix="/api/stripe")
    app.include_router(admin.router,            prefix="/api/admin")
    app.include_router(lyrics.router,           prefix="/api/lyrics")       # NEW
    app.include_router(messages.router,         prefix="/api/messages")     # NEW

MongoDB indexes to create (run once):

    db.follows.create_index([("follower_id", 1), ("following_id", 1)], unique=True)
    db.messages.create_index([("from_username", 1), ("to_username", 1), ("created_at", -1)])
    db.messages.create_index([("to_username", 1), ("read", 1)])
    db.lyrics.create_index([("user_id", 1), ("created_at", -1)])
    db.users.create_index([("username", 1)], unique=True, sparse=True)
    db.users.create_index([("email", 1)], unique=True)
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os

from routes import auth, beats, youtube, producer, stripe_payments, admin
from routes import lyrics, messages   # new modules

app = FastAPI(title="BeatFinder API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME   = os.getenv("DB_NAME", "beatfinder")

@app.on_event("startup")
async def startup():
    client = AsyncIOMotorClient(MONGO_URI)
    app.state.db = client[DB_NAME]

# ── Existing routers ──────────────────────────────────────────────
app.include_router(auth.router,            prefix="/api/auth")
app.include_router(beats.router,           prefix="/api/beats")
app.include_router(youtube.router,         prefix="/api/youtube")
app.include_router(producer.router,        prefix="/api/producer")
app.include_router(stripe_payments.router, prefix="/api/stripe")
app.include_router(admin.router,           prefix="/api/admin")

# ── New routers ───────────────────────────────────────────────────
app.include_router(lyrics.router,   prefix="/api/lyrics")
app.include_router(messages.router, prefix="/api/messages")

@app.get("/")
async def root():
    return {"status": "ok", "service": "BeatFinder API"}
