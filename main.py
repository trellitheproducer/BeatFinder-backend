"""
BeatFinder Backend - FastAPI + MongoDB + YouTube Data API
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import os

from routes.auth import router as auth_router
from routes.beats import router as beats_router
from routes.youtube import router as youtube_router
from routes.admin import router as admin_router
from routes.producer import router as producer_router
from routes.stripe_payments import router as stripe_router
from routes.lyrics import router as lyrics_router
from routes.messages import router as messages_router
from routes.content  import router as content_router
from routes.posts    import router as posts_router
from routes.ai import router as ai_router
from routes.notifications import router as notifications_router
from routes.contracts import router as contracts_router
from routes.bpm import router as bpm_router
from routes.support_routes import router as support_router

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.mongo = AsyncIOMotorClient(os.getenv("MONGODB_URI"))
    app.state.db    = app.state.mongo[os.getenv("MONGODB_DB", "beatfinder")]
    print("MongoDB connected")
    await app.state.db.yt_cache.create_index("cached_at")
    await app.state.db.yt_cache.create_index([("_id", 1)])
    await app.state.db.lyrics.create_index([("user_id", 1), ("lyric_id", 1)], unique=True)
    await app.state.db.lyrics.create_index([("user_id", 1), ("updated_at", -1)])
    await app.state.db.follows.create_index([("follower_id", 1), ("following_id", 1)], unique=True)
    await app.state.db.messages.create_index([("from_username", 1), ("to_username", 1), ("created_at", -1)])
    await app.state.db.messages.create_index([("to_username", 1), ("read", 1)])
    await app.state.db.content.create_index([("username", 1), ("type", 1), ("createdAt", -1)])
    await app.state.db.content_likes.create_index([("contentId", 1), ("username", 1)], unique=True)
    await app.state.db.content_comments.create_index([("contentId", 1), ("createdAt", 1)])
    await app.state.db.posts.create_index([("username", 1), ("type", 1), ("createdAt", -1)])
    await app.state.db.post_likes.create_index([("postId", 1), ("username", 1)], unique=True)
    await app.state.db.post_comments.create_index([("postId", 1), ("createdAt", 1)])
    await app.state.db.notifications.create_index([("toUser", 1), ("read", 1), ("createdAt", -1)])
    await app.state.db.contract_acceptances.create_index([("licensee_id", 1), ("accepted_at", -1)])
    # BPM analysis cache — keyed by file SHA-256, auto-expires after 30d
    await app.state.db.bpm_cache.create_index("created_at", expireAfterSeconds=30 * 24 * 3600)
    print("Indexes ready")
    yield
    app.state.mongo.close()
    print("MongoDB disconnected")


app = FastAPI(title="BeatFinder API", version="1.0.0", lifespan=lifespan)

ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:5173"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router,            prefix="/api/auth",           tags=["Auth"])
app.include_router(beats_router,           prefix="/api/beats",          tags=["Saved Beats"])
app.include_router(youtube_router,         prefix="/api/youtube",        tags=["YouTube"])
app.include_router(admin_router,           prefix="/api/admin",          tags=["Admin"])
app.include_router(producer_router,        prefix="/api/producer",       tags=["Producer Beats"])
app.include_router(lyrics_router,          prefix="/api/lyrics",         tags=["Lyrics"])
app.include_router(messages_router,        prefix="/api/messages",       tags=["Messages"])
app.include_router(content_router,         prefix="/api/content",        tags=["Content"])
app.include_router(posts_router,           prefix="/api/posts",          tags=["Posts"])
app.include_router(ai_router,              prefix="/api/ai",             tags=["AI"])
app.include_router(notifications_router,   prefix="/api/notifications",  tags=["Notifications"])
app.include_router(contracts_router,       prefix="/api/contracts",      tags=["Contracts"])
app.include_router(bpm_router,             prefix="/api/bpm",            tags=["BPM Detection"])
app.include_router(support_router,         prefix="/api",                tags=["Support"])

from routes.producer import lease_webhook
app.post("/api/producer/lease-webhook")(lease_webhook)
app.include_router(stripe_router, prefix="/api/stripe", tags=["Stripe Payments"])


@app.get("/")
async def root():
    return {"status": "ok", "service": "BeatFinder API v1.0"}

@app.get("/health")
async def health():
    return {"status": "healthy"}
