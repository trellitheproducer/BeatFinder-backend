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
    Returns the decoded token payload {"sub": user_id, "email": ...}
    """
    payload = decode_token(credentials.credentials)
    db      = request.app.state.db

    user = await db.users.find_one({"_id": payload["sub"]})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return user


async def get_admin_user(user=Depends(get_current_user)):
    """Dependency for admin-only routes."""
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
