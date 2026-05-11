"""
Notifications routes: /api/notifications
Real-time activity feed: likes, comments, purchases, messages.
"""

from fastapi import APIRouter, Request, Depends
from datetime import datetime
from bson import ObjectId

from auth import get_current_user

router = APIRouter()


def _notif_out(doc):
    return {
        "id":        str(doc.get("_id", "")),
        "type":      doc.get("type", ""),       # like | comment | purchase | message
        "fromUser":  doc.get("fromUser", ""),
        "text":      doc.get("text", ""),
        "read":      doc.get("read", False),
        "createdAt": doc.get("createdAt", datetime.utcnow()).isoformat()
                     if hasattr(doc.get("createdAt"), "isoformat") else str(doc.get("createdAt", "")),
    }


@router.get("/")
async def get_notifications(request: Request, user=Depends(get_current_user)):
    db       = request.app.state.db
    username = user.get("username", "")
    docs = await db.notifications.find(
        {"toUser": username}
    ).sort("createdAt", -1).to_list(50)
    return [_notif_out(d) for d in docs]


@router.get("/unread-count")
async def unread_count(request: Request, user=Depends(get_current_user)):
    db       = request.app.state.db
    username = user.get("username", "")
    count = await db.notifications.count_documents({"toUser": username, "read": False})
    # Also count unread messages
    msg_pipeline = [
        {"$match": {"to_username": username, "read": False}},
        {"$count": "total"}
    ]
    msg_result = await db.messages.aggregate(msg_pipeline).to_list(1)
    msg_unread = msg_result[0]["total"] if msg_result else 0
    return {"notifications": count, "messages": msg_unread, "total": count + msg_unread}


@router.post("/mark-read")
async def mark_all_read(request: Request, user=Depends(get_current_user)):
    db       = request.app.state.db
    username = user.get("username", "")
    await db.notifications.update_many(
        {"toUser": username, "read": False},
        {"$set": {"read": True}}
    )
    return {"ok": True}


@router.post("/{notif_id}/read")
async def mark_one_read(notif_id: str, request: Request, user=Depends(get_current_user)):
    db = request.app.state.db
    await db.notifications.update_one(
        {"_id": notif_id, "toUser": user.get("username")},
        {"$set": {"read": True}}
    )
    return {"ok": True}


# ── Internal helper — called by other routes when events happen ──────────────
async def create_notification(db, to_user: str, from_user: str, type_: str, text: str):
    """Create a notification. Skips self-notifications."""
    if not to_user or to_user == from_user:
        return
    doc = {
        "_id":       str(ObjectId()),
        "toUser":    to_user,
        "fromUser":  from_user,
        "type":      type_,
        "text":      text,
        "read":      False,
        "createdAt": datetime.utcnow(),
    }
    await db.notifications.insert_one(doc)
