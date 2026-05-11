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
        "id":          str(doc["_id"]),
        "from":        doc.get("from_username", ""),
        "to":          doc.get("to_username", ""),
        "text":        doc.get("text", ""),
        "unsent":      doc.get("unsent", False),
        "createdAt":   doc["created_at"].isoformat() if hasattr(doc.get("created_at"), "isoformat") else str(doc.get("created_at", "")),
        "read":        doc.get("read", False),
        "delivered":   doc.get("delivered", True),  # all stored msgs are delivered
    }


# ── Get all conversations ─────────────────────────────────────────────────────
@router.get("/conversations")
async def get_conversations(request: Request, user=Depends(get_current_user)):
    db       = request.app.state.db
    username = user.get("username", "")
    if not username:
        return []

    pipeline = [
        {"$match": {"$or": [{"from_username": username}, {"to_username": username}]}},
        {"$sort": {"created_at": -1}},
        {"$group": {
            "_id": {"$cond": {"if": {"$eq": ["$from_username", username]}, "then": "$to_username", "else": "$from_username"}},
            "lastMessage": {"$first": {"$cond": [{"$eq": ["$unsent", True]}, "Message unsent", "$text"]}},
            "lastAt":      {"$first": "$created_at"},
            "unread": {"$sum": {"$cond": [{"$and": [{"$eq": ["$to_username", username]}, {"$eq": ["$read", False]}, {"$ne": ["$unsent", True]}]}, 1, 0]}}
        }},
        {"$sort": {"lastAt": -1}},
        {"$limit": 50},
    ]

    results = await db.messages.aggregate(pipeline).to_list(50)

    # Fetch avatar URLs for each conversation partner
    usernames = [r["_id"] for r in results if r["_id"]]
    avatar_map = {}
    if usernames:
        users = await db.users.find({"username": {"$in": usernames}}, {"username": 1, "avatarUrl": 1}).to_list(50)
        for u in users:
            avatar_map[u["username"]] = u.get("avatarUrl", "")

    return [
        {
            "username":    r["_id"],
            "lastMessage": r.get("lastMessage", ""),
            "lastAt":      r["lastAt"].isoformat() if hasattr(r.get("lastAt"), "isoformat") else "",
            "unread":      r.get("unread", 0),
            "avatarUrl":   avatar_map.get(r["_id"], ""),
        }
        for r in results
    ]


# ── Get thread + mark received messages as read ───────────────────────────────
@router.get("/thread/{other_username}")
async def get_thread(other_username: str, request: Request, user=Depends(get_current_user)):
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

    # Mark received messages as read (triggers "read" status for sender)
    await db.messages.update_many(
        {"from_username": other_username, "to_username": username, "read": False},
        {"$set": {"read": True}}
    )

    return [_msg_out(m) for m in messages]


# ── Send a message ─────────────────────────────────────────────────────────────
class SendMessageBody(BaseModel):
    to:   str
    text: str

@router.post("/send", status_code=201)
async def send_message(body: SendMessageBody, request: Request, user=Depends(get_current_user)):
    db       = request.app.state.db
    username = user.get("username", "")
    if not username:
        raise HTTPException(status_code=400, detail="Set a username before messaging")

    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(text) > 2000:
        raise HTTPException(status_code=400, detail="Message too long (max 2000 chars)")

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
        "delivered":     True,
        "unsent":        False,
    }
    await db.messages.insert_one(doc)
    return _msg_out(doc)


# ── Unsend a message (own messages only) ──────────────────────────────────────
@router.delete("/{message_id}/unsend")
async def unsend_message(message_id: str, request: Request, user=Depends(get_current_user)):
    db       = request.app.state.db
    username = user.get("username", "")

    result = await db.messages.update_one(
        {"_id": message_id, "from_username": username},
        {"$set": {"unsent": True, "text": ""}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Message not found or not yours")
    return {"unsent": True, "id": message_id}


# ── Delete entire thread ──────────────────────────────────────────────────────
@router.delete("/thread/{other_username}")
async def delete_thread(other_username: str, request: Request, user=Depends(get_current_user)):
    db       = request.app.state.db
    username = user.get("username", "")

    await db.messages.delete_many({
        "$or": [
            {"from_username": username, "to_username": other_username},
            {"from_username": other_username, "to_username": username},
        ]
    })
    return {"deleted": True}


# ── Poll for new messages in a thread (lightweight) ───────────────────────────
@router.get("/thread/{other_username}/updates")
async def poll_thread(other_username: str, after: str = None, request: Request = None, user=Depends(get_current_user)):
    """Returns messages newer than `after` ISO timestamp. Used for read receipts + new messages."""
    db       = request.app.state.db
    username = user.get("username", "")

    query = {
        "$or": [
            {"from_username": username, "to_username": other_username},
            {"from_username": other_username, "to_username": username},
        ]
    }
    if after:
        try:
            from datetime import timezone
            after_dt = datetime.fromisoformat(after.replace("Z", "+00:00")).replace(tzinfo=None)
            query["created_at"] = {"$gt": after_dt}
        except Exception:
            pass

    msgs = await db.messages.find(query).sort("created_at", 1).to_list(50)

    # Also return updated read status for messages we sent
    read_updates = await db.messages.find(
        {"from_username": username, "to_username": other_username, "read": True},
        {"_id": 1, "read": 1}
    ).to_list(200)

    await db.messages.update_many(
        {"from_username": other_username, "to_username": username, "read": False},
        {"$set": {"read": True}}
    )

    return {
        "messages":    [_msg_out(m) for m in msgs],
        "readIds":     [str(r["_id"]) for r in read_updates],
    }
