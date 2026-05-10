"""
Messages routes: /api/messages
Direct messaging between users. Requires authentication.
"""

from fastapi import APIRouter, Request, Depends, HTTPException
from datetime import datetime
from pydantic import BaseModel
from bson import ObjectId

from auth import get_current_user

router = APIRouter()


def _msg_out(doc: dict) -> dict:
    return {
        "id":        str(doc["_id"]),
        "from":      doc.get("from_username", ""),
        "to":        doc.get("to_username", ""),
        "text":      doc.get("text", ""),
        "createdAt": doc["created_at"].isoformat() if hasattr(doc.get("created_at"), "isoformat") else str(doc.get("created_at", "")),
        "read":      doc.get("read", False),
    }


# ── Get all conversations for current user ────────────────────────
@router.get("/conversations")
async def get_conversations(request: Request, user=Depends(get_current_user)):
    db       = request.app.state.db
    username = user.get("username", "")
    if not username:
        return []

    # Find all messages where user is sender or receiver
    pipeline = [
        {
            "$match": {
                "$or": [
                    {"from_username": username},
                    {"to_username":   username},
                ]
            }
        },
        {"$sort": {"created_at": -1}},
        {
            "$group": {
                "_id": {
                    "$cond": {
                        "if":   {"$eq": ["$from_username", username]},
                        "then": "$to_username",
                        "else": "$from_username",
                    }
                },
                "lastMessage": {"$first": "$text"},
                "lastAt":      {"$first": "$created_at"},
                "unread": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$to_username", username]},
                                    {"$eq": ["$read", False]},
                                ]
                            },
                            1, 0
                        ]
                    }
                }
            }
        },
        {"$sort": {"lastAt": -1}},
        {"$limit": 50},
    ]

    results = await db.messages.aggregate(pipeline).to_list(50)
    return [
        {
            "username":    r["_id"],
            "lastMessage": r.get("lastMessage", ""),
            "lastAt":      r["lastAt"].isoformat() if hasattr(r.get("lastAt"), "isoformat") else "",
            "unread":      r.get("unread", 0),
        }
        for r in results
    ]


# ── Get message thread between current user and another user ──────
@router.get("/thread/{other_username}")
async def get_thread(
    other_username: str,
    request: Request,
    user=Depends(get_current_user),
):
    db       = request.app.state.db
    username = user.get("username", "")
    if not username:
        raise HTTPException(status_code=400, detail="Set a username first")

    messages = await db.messages.find({
        "$or": [
            {"from_username": username, "to_username": other_username},
            {"from_username": other_username, "to_username": username},
        ]
    }).sort("created_at", 1).to_list(200)

    # Mark received messages as read
    await db.messages.update_many(
        {"from_username": other_username, "to_username": username, "read": False},
        {"$set": {"read": True}}
    )

    return [_msg_out(m) for m in messages]


# ── Send a message ────────────────────────────────────────────────
class SendMessageBody(BaseModel):
    to:   str
    text: str

@router.post("/send", status_code=201)
async def send_message(
    body: SendMessageBody,
    request: Request,
    user=Depends(get_current_user),
):
    db       = request.app.state.db
    username = user.get("username", "")
    if not username:
        raise HTTPException(status_code=400, detail="Set a username before messaging")

    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(text) > 2000:
        raise HTTPException(status_code=400, detail="Message too long (max 2000 chars)")

    # Check recipient exists
    recipient = await db.users.find_one({"username": body.to})
    if not recipient:
        raise HTTPException(status_code=404, detail="User not found")

    if body.to == username:
        raise HTTPException(status_code=400, detail="Cannot message yourself")

    doc = {
        "_id":           str(ObjectId()),
        "from_username": username,
        "to_username":   body.to,
        "text":          text,
        "created_at":    datetime.utcnow(),
        "read":          False,
    }
    await db.messages.insert_one(doc)
    return _msg_out(doc)
