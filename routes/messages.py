"""
Messages routes: /api/messages
Direct messaging between users. Requires authentication.

INBOX vs REQUESTS — INSTAGRAM-STYLE GATE
────────────────────────────────────────
A conversation is either in your INBOX or your REQUESTS folder.
The rule for which folder it goes into, from user A's perspective
looking at a thread with user B:

  INBOX if any of:
    • A follows B (then incoming from B is auto-accepted)
    • A has previously sent a message to B (initiating a thread
      is implicit acceptance)
    • A has explicitly accepted the request via the Accept button

  REQUESTS otherwise.

When user A follows user B, the follow endpoint (over in auth.py)
marks the thread as accepted from A's side automatically — that
implements "accepting a request by following back."

Schema notes:
  • Per-message: nothing new is stored. Status is derived per-user
    per-thread at query time.
  • Per-thread acceptance state is stored in db.message_threads:
        {
          _id:           "alphabetically-sorted-pair",
          users:         [userA, userB],
          accepted_by:   [userA]      // who has explicitly accepted
        }
    Missing doc = no explicit acceptance from either side. Whether
    the thread shows in inbox is then determined by the follow
    relationship + sender-history.
"""

from fastapi import APIRouter, Request, Depends, HTTPException
from datetime import datetime
from pydantic import BaseModel
from bson import ObjectId

from auth import get_current_user

router = APIRouter()


def _thread_id(user_a: str, user_b: str) -> str:
    """Stable, order-independent thread id for two usernames."""
    a, b = sorted([user_a, user_b])
    return a + ":" + b


def _msg_out(doc: dict) -> dict:
    return {
        "id":          str(doc["_id"]),
        "from":        doc.get("from_username", ""),
        "to":          doc.get("to_username", ""),
        "text":        doc.get("text", ""),
        "unsent":      doc.get("unsent", False),
        "createdAt":   doc["created_at"].isoformat() if hasattr(doc.get("created_at"), "isoformat") else str(doc.get("created_at", "")),
        "read":        doc.get("read", False),
        "delivered":   doc.get("delivered", True),
    }


async def _is_in_inbox(db, me: str, them: str) -> bool:
    """
    True if the thread between `me` and `them` should appear in MY
    inbox (vs my requests). See the module docstring for the rule.

    The three signals checked, in cheapest order first:
      1. I have explicitly accepted in db.message_threads
      2. I follow them (so anything they send is implicitly welcome)
      3. I have previously sent them a message (starting a thread = accept)
    """
    # 1. Explicit acceptance (cheapest — single keyed lookup)
    tid = _thread_id(me, them)
    thread = await db.message_threads.find_one({"_id": tid})
    if thread and me in (thread.get("accepted_by") or []):
        return True

    # 2. Do I follow them? Look up by username → user id, then check follows.
    them_user = await db.users.find_one({"username": them}, {"_id": 1})
    me_user   = await db.users.find_one({"username": me},   {"_id": 1})
    if them_user and me_user:
        is_following = await db.follows.find_one({
            "follower_id":  str(me_user["_id"]),
            "following_id": str(them_user["_id"]),
        })
        if is_following:
            return True

    # 3. Have I ever sent them a message? Initiating = accepting.
    sent_one = await db.messages.find_one({
        "from_username": me,
        "to_username":   them,
    }, {"_id": 1})
    if sent_one:
        return True

    return False


async def _mark_accepted(db, me: str, them: str) -> None:
    """
    Mark this thread as explicitly accepted by `me`. Idempotent.
    Used when:
      • user manually taps Accept on a request
      • user follows the other party (auto-accept on follow-back,
        called from the follow endpoint)
      • user replies to a pending message (replying = accepting)
    """
    tid = _thread_id(me, them)
    await db.message_threads.update_one(
        {"_id": tid},
        {
            "$setOnInsert": {"users": sorted([me, them])},
            "$addToSet":    {"accepted_by": me},
        },
        upsert=True,
    )


# ── Get conversations (INBOX only) ────────────────────────────────────────────
@router.get("/conversations")
async def get_conversations(request: Request, user=Depends(get_current_user)):
    """
    Returns the user's INBOX — threads they've accepted, threads
    where they follow the other party, and threads they initiated.
    Pending request threads are returned separately by /requests.
    """
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

    # Split results into inbox vs requests based on the gate rule
    inbox = []
    for r in results:
        other = r["_id"]
        if not other:
            continue
        if await _is_in_inbox(db, username, other):
            inbox.append(r)

    # Fetch avatar URLs for inbox partners only
    usernames = [r["_id"] for r in inbox if r["_id"]]
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
        for r in inbox
    ]


# ── Get message REQUESTS (pending threads not in inbox) ───────────────────────
@router.get("/requests")
async def get_requests(request: Request, user=Depends(get_current_user)):
    """
    Returns threads currently sitting in the user's Requests folder
    (incoming messages from people they don't follow and haven't
    accepted). Same shape as /conversations.
    """
    db       = request.app.state.db
    username = user.get("username", "")
    if not username:
        return []

    pipeline = [
        # Only threads where I received messages — outbound starts
        # never go to requests because sending = accepting from my side.
        {"$match": {"to_username": username}},
        {"$sort": {"created_at": -1}},
        {"$group": {
            "_id":         "$from_username",
            "lastMessage": {"$first": {"$cond": [{"$eq": ["$unsent", True]}, "Message unsent", "$text"]}},
            "lastAt":      {"$first": "$created_at"},
            "unread":      {"$sum": {"$cond": [{"$and": [{"$eq": ["$read", False]}, {"$ne": ["$unsent", True]}]}, 1, 0]}},
        }},
        {"$sort": {"lastAt": -1}},
        {"$limit": 50},
    ]

    results = await db.messages.aggregate(pipeline).to_list(50)

    # Keep only the ones NOT in inbox (the inverse of /conversations)
    pending = []
    for r in results:
        other = r["_id"]
        if not other:
            continue
        if not await _is_in_inbox(db, username, other):
            pending.append(r)

    usernames = [r["_id"] for r in pending if r["_id"]]
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
        for r in pending
    ]


# ── Get summary counts (used by Messages top-bar badge) ──────────────────────
@router.get("/summary")
async def get_summary(request: Request, user=Depends(get_current_user)):
    """
    Lightweight counts used by the top-bar message badge.
    Returns:
      inboxUnread:    sum of unread across INBOX threads
      requestsCount:  number of pending request threads
    The top-bar message bubble badge should reflect inboxUnread only
    — pending requests get their own indicator inside the messages
    screen, matching Instagram's UX.
    """
    db       = request.app.state.db
    username = user.get("username", "")
    if not username:
        return {"inboxUnread": 0, "requestsCount": 0}

    # Find all senders who messaged me
    pipeline = [
        {"$match": {"to_username": username, "read": False, "unsent": {"$ne": True}}},
        {"$group": {"_id": "$from_username", "n": {"$sum": 1}}},
    ]
    rows = await db.messages.aggregate(pipeline).to_list(200)

    inbox_unread = 0
    requests_senders = set()
    for r in rows:
        sender = r["_id"]
        if not sender: continue
        if await _is_in_inbox(db, username, sender):
            inbox_unread += r["n"]
        else:
            requests_senders.add(sender)

    return {
        "inboxUnread":   inbox_unread,
        "requestsCount": len(requests_senders),
    }


# ── Accept a pending request manually ─────────────────────────────────────────
@router.post("/requests/{other_username}/accept")
async def accept_request(other_username: str, request: Request, user=Depends(get_current_user)):
    """Move a pending thread into the inbox."""
    db       = request.app.state.db
    username = user.get("username", "")
    if not username or other_username == username:
        raise HTTPException(status_code=400, detail="Invalid request")
    # Verify there's actually a thread between us
    exists = await db.messages.find_one({
        "$or": [
            {"from_username": other_username, "to_username": username},
            {"from_username": username, "to_username": other_username},
        ]
    }, {"_id": 1})
    if not exists:
        raise HTTPException(status_code=404, detail="No thread with that user")
    await _mark_accepted(db, username, other_username)
    return {"accepted": True}


# ── Decline a pending request (deletes the thread) ────────────────────────────
@router.delete("/requests/{other_username}")
async def decline_request(other_username: str, request: Request, user=Depends(get_current_user)):
    """
    Delete a pending request thread. Removes all messages between
    the two users (matches Instagram's "Delete" on a request).
    """
    db       = request.app.state.db
    username = user.get("username", "")
    await db.messages.delete_many({
        "$or": [
            {"from_username": username, "to_username": other_username},
            {"from_username": other_username, "to_username": username},
        ]
    })
    # Clear any thread-acceptance state too (it'd be stale otherwise)
    await db.message_threads.delete_one({"_id": _thread_id(username, other_username)})
    return {"declined": True}


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

    # Mark received messages as read
    await db.messages.update_many(
        {"from_username": other_username, "to_username": username, "read": False},
        {"$set": {"read": True}}
    )

    # Whether this thread is currently in MY inbox (vs requests).
    # The client uses this to decide whether to show Accept/Decline
    # buttons above the input.
    in_inbox = await _is_in_inbox(db, username, other_username)

    return {
        "messages":  [_msg_out(m) for m in messages],
        "inInbox":   in_inbox,
    }


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

    # Sending a message implicitly accepts the thread from MY side
    # (otherwise an outbound reply could land in my own requests folder,
    # which makes no sense). The RECIPIENT'S inbox-vs-requests state
    # is decided at read-time based on whether they follow us.
    await _mark_accepted(db, username, body.to)

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
    # Also clear thread acceptance state
    await db.message_threads.delete_one({"_id": _thread_id(username, other_username)})
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
            after_dt = datetime.fromisoformat(after.replace("Z", "+00:00")).replace(tzinfo=None)
            query["created_at"] = {"$gt": after_dt}
        except Exception:
            pass

    msgs = await db.messages.find(query).sort("created_at", 1).to_list(50)

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
