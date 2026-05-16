"""
JWT auth helpers — used by all protected routes.
"""

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
import os

SECRET_KEY   = os.getenv("JWT_SECRET", "change-this-in-production-please")
ALGORITHM    = "HS256"
TOKEN_EXPIRE = int(os.getenv("JWT_EXPIRE_MINUTES", "10080"))  # 7 days

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer      = HTTPBearer()


def hash_password(password: str) -> str:
    # bcrypt limit is 72 bytes - truncate safely
    pw = password.encode("utf-8")[:72].decode("utf-8", errors="ignore")
    return pwd_context.hash(pw)


def verify_password(plain: str, hashed: str) -> bool:
    pw = plain.encode("utf-8")[:72].decode("utf-8", errors="ignore")
    return pwd_context.verify(pw, hashed)


def create_token(user_id: str, email: str) -> str:
    payload = {
        "sub":   user_id,
        "email": email,
        "exp":   datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
):
    """
    Dependency — inject into any route that needs auth.
    Returns the user doc from MongoDB.

    Looks up the user via find_user_by_id which transparently handles
    both `_id` formats (string for newer accounts, ObjectId for legacy).
    Without this both-format lookup, legacy accounts would 404 on every
    authenticated request, locking them out of the entire API.
    """
    from db_helpers import find_user_by_id
    payload = decode_token(credentials.credentials)
    db      = request.app.state.db

    user = await find_user_by_id(db, payload["sub"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return user


async def get_admin_user(user=Depends(get_current_user)):
    """Dependency for admin-only routes."""
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── Lifetime / effective-plan helpers ────────────────────────────────
# These need to live in this root-level auth.py (not routes/auth.py)
# because they're imported by routes/posts.py, routes/producer.py, etc.
# via `from auth import ...` which resolves to this file, not routes/auth.py.
#
# The hardcoded LIFETIME_ACCOUNTS list is duplicated between this file
# and routes/auth.py — that's intentional. Keeping both in sync requires
# editing two places, but it avoids circular imports between root and
# routes modules. If you change one, change the other.
_LIFETIME_ACCOUNTS_ROOT = {
    "Trelli":     {"plan": "producer", "is_admin": True},
    "Mikez":      {"plan": "artist",   "is_admin": False},
    "HMbarsdat":  {"plan": "artist",   "is_admin": False},
}


async def _lifetime_config_async_root(db, username: str):
    """Same logic as routes/auth.py's _lifetime_config_async, but lives
    here so the helpers below can be imported without circular deps."""
    if not username:
        return None
    hard = _LIFETIME_ACCOUNTS_ROOT.get(username)
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
    features via the LIFETIME_ACCOUNTS dict (hardcoded) or
    db.lifetime_accounts (admin-granted).

    Usage:
        if await get_effective_plan(db, user) not in ("artist", "producer"):
            raise HTTPException(403, "Pro plan required")

    Returns the plan string: "artist" / "producer" / "free".
    """
    if not user:
        return "free"
    cfg = await _lifetime_config_async_root(db, user.get("username", ""))
    if cfg:
        return cfg.get("plan", "artist")
    return user.get("plan", "free")
