"""
db_helpers — shared MongoDB user lookup helpers.

Background
──────────
BeatFinder has two generations of user records:
  • Legacy accounts (pre-launch) have `_id` stored as a BSON ObjectId.
  • Newer accounts (post-username-flow) have `_id` stored as a plain
    string — generated as `str(ObjectId())` at registration time.

Code that looked up users with a single-format query was silently
breaking for one half of the userbase. We had multiple production
bugs from this:
  • get_current_user → legacy users locked out
  • reset-password   → silent no-op for new accounts
  • buy-lease        → new producers couldn't sell beats
  • beat-play count  → wrong cohort tracked

These helpers centralise the both-format lookup so every new feature
gets the correct behavior by default. Always use these for user `_id`
lookups instead of writing the both-format try/except inline.

Usage
─────
    from db_helpers import find_user_by_id, update_user_by_id

    user = await find_user_by_id(db, some_id)
    if not user:
        raise HTTPException(404, "User not found")

    await update_user_by_id(db, some_id, {"$set": {"bio": "new bio"}})

Notes
─────
- `some_id` can be a string or already an ObjectId — both work.
- Returns native Mongo result objects, so existing code that reads
  `.modified_count` / `.matched_count` / `.deleted_count` keeps working.
- If the input id is None or empty, helpers return None / a no-op
  result rather than raising, so guard callers don't need to.
"""

from bson import ObjectId
from bson.errors import InvalidId


def _as_object_id(user_id):
    """Best-effort ObjectId conversion. Returns the ObjectId or None
    if the input can't be parsed. Never raises."""
    if user_id is None:
        return None
    if isinstance(user_id, ObjectId):
        return user_id
    try:
        return ObjectId(str(user_id))
    except (InvalidId, TypeError, ValueError):
        return None


async def find_user_by_id(db, user_id):
    """Find a user by `_id`, trying string format first and falling back
    to ObjectId. Returns the user doc or None if not found / invalid id."""
    if not user_id:
        return None
    # String-first since newer accounts are the majority going forward
    user = await db.users.find_one({"_id": str(user_id)}) if not isinstance(user_id, ObjectId) else None
    if user:
        return user
    oid = _as_object_id(user_id)
    if oid is None:
        return None
    return await db.users.find_one({"_id": oid})


async def update_user_by_id(db, user_id, update_doc, upsert=False):
    """Update a user by `_id`, trying string format first and falling
    back to ObjectId on zero matches. Returns the Mongo UpdateResult.

    `update_doc` should be a Mongo update operator dict like
    {"$set": {...}} — same as you'd pass to update_one directly.
    """
    # Mimic update_one's return-shape for the not-found / invalid-id case
    class _NoOp:
        modified_count = 0
        matched_count  = 0
        upserted_id    = None
    if not user_id:
        return _NoOp()

    # Try string match first
    if not isinstance(user_id, ObjectId):
        result = await db.users.update_one(
            {"_id": str(user_id)},
            update_doc,
            upsert=False,  # never upsert on the string path — we want to
                           # know if it missed, so we can try ObjectId
        )
        if result.matched_count > 0:
            return result

    # Fall back to ObjectId
    oid = _as_object_id(user_id)
    if oid is None:
        return _NoOp()
    return await db.users.update_one({"_id": oid}, update_doc, upsert=upsert)


async def delete_user_by_id(db, user_id):
    """Delete a user by `_id`, trying string format first and falling
    back to ObjectId. Returns the Mongo DeleteResult."""
    class _NoOp:
        deleted_count = 0
    if not user_id:
        return _NoOp()

    if not isinstance(user_id, ObjectId):
        result = await db.users.delete_one({"_id": str(user_id)})
        if result.deleted_count > 0:
            return result

    oid = _as_object_id(user_id)
    if oid is None:
        return _NoOp()
    return await db.users.delete_one({"_id": oid})


def _normalize_username(username):
    """Local copy of normalize_username for circular-import-free use.
    Same logic as auth.py's normalize_username: lowercase + strip.
    If you change one, change both."""
    if not username or not isinstance(username, str):
        return ""
    return username.strip().lower()


async def find_user_by_username(db, username, *, projection=None):
    """Case-insensitive username lookup.

    Strategy:
      1. Try normalized_username (fast, uses index)
      2. Fall back to a case-insensitive regex on `username`
         (covers any user not yet backfilled, also a safety net)

    The fallback is bounded — we only call it if the first lookup
    missed AND the username is non-empty. So worst case it's one
    extra DB query, not a scan storm.

    Args:
        db: motor database
        username: any case ("Trelli", "trelli", "TRELLI")
        projection: optional dict for find_one projection

    Returns:
        User doc or None.

    Display casing is preserved on the returned doc — only the
    LOOKUP is case-insensitive. The `username` field on the
    returned user is whatever they originally registered with.
    """
    if not username:
        return None
    norm = _normalize_username(username)
    if not norm:
        return None

    # Path 1 — normalized field (fast, indexed)
    query = {"normalized_username": norm}
    if projection:
        user = await db.users.find_one(query, projection)
    else:
        user = await db.users.find_one(query)
    if user:
        return user

    # Path 2 — case-insensitive regex fallback for not-yet-backfilled users.
    # We escape regex specials to avoid issues with usernames containing dots
    # or other regex chars (though usernames shouldn't have them, defence
    # in depth never hurts).
    import re
    escaped = re.escape(username)
    query = {"username": {"$regex": "^" + escaped + "$", "$options": "i"}}
    if projection:
        return await db.users.find_one(query, projection)
    return await db.users.find_one(query)
