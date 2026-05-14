"""
follower_notify.py — bundled post notifications for followers.

Drops a Twitter/X-style notification into the existing `notifications`
collection for every follower of the poster, using the same schema your
notifications.py route already reads. Bundles repeated posts within a
6-hour window so we don't spam ("@alice posted 3 new updates").

Notification doc shape — extends your existing schema with extra fields:

  {
    _id, toUser, fromUser, type="post", text,
    read=False, createdAt,
    # ── new ───────────────────────────────────────
    postId,      # the latest post id (for tap-to-jump)
    postType,    # "status" | "image" | "music" | "video" | "mixed"
    count,       # 1 or more (incremented on each new post within bundle window)
    updatedAt,
  }

USAGE in posts.py (one line per post-create endpoint):

    from routes.follower_notify import notify_post_to_followers
    await notify_post_to_followers(request.app.state.db, user, doc["_id"], "status")
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict
from bson import ObjectId

logger = logging.getLogger("follower_notify")

# Bundle window — within this period, additional posts from the same
# person update the existing notification instead of creating a new one.
BUNDLE_WINDOW_HOURS = 6


def _format_text(username: str, count: int, post_type: str) -> str:
    if count == 1:
        type_phrases = {
            "status": "posted an update",
            "image":  "shared a new photo",
            "music":  "shared a new track",
            "video":  "posted a new video",
        }
        phrase = type_phrases.get(post_type, "posted an update")
        return f"@{username} {phrase}"
    return f"@{username} posted {count} new updates"


async def notify_post_to_followers(
    db,
    poster: Dict[str, Any],
    post_id: str,
    post_type: str,
) -> None:
    """Send a bundled 'new post' notification to every follower.

    Args:
        db:        Motor AsyncIOMotorDatabase
        poster:    The user doc of whoever made the post (from get_current_user).
        post_id:   The new post's _id (used as the tap-to-jump target).
        post_type: "status" | "image" | "music" | "video"

    All errors are swallowed — this must never break the post-create flow.
    """
    try:
        poster_id  = str(poster.get("_id") or "")
        poster_un  = (poster.get("username") or "").strip()
        if not poster_id or not poster_un:
            return

        # Find every user following this poster.
        # follows: { follower_id: str, following_id: str } — both are user _id strings
        follow_docs = await db.follows.find({"following_id": poster_id}).to_list(length=10_000)
        follower_ids = [f.get("follower_id") for f in follow_docs if f.get("follower_id")]
        if not follower_ids:
            return

        # Look up each follower's USERNAME (notifications.toUser stores
        # username, not _id — matches your existing schema).
        follower_users = await db.users.find(
            {"_id": {"$in": follower_ids}},
            {"username": 1, "_id": 1},
        ).to_list(length=10_000)
        follower_usernames = [u.get("username") for u in follower_users if u.get("username")]
        if not follower_usernames:
            return

        now    = datetime.utcnow()
        cutoff = now - timedelta(hours=BUNDLE_WINDOW_HOURS)

        for fun in follower_usernames:
            # Paranoid guard — don't notify the poster themselves
            if fun == poster_un:
                continue

            # Bundle: existing UNREAD "post" notification from this poster
            # within the last 6 hours? If yes, update it. Else create new.
            existing = await db.notifications.find_one({
                "toUser":   fun,
                "type":     "post",
                "fromUser": poster_un,
                "read":     False,
                "createdAt": {"$gte": cutoff},
            })

            if existing:
                new_count = int(existing.get("count", 1)) + 1
                await db.notifications.update_one(
                    {"_id": existing["_id"]},
                    {"$set": {
                        "count":     new_count,
                        "text":      _format_text(poster_un, new_count, "mixed"),
                        "postId":    post_id,
                        "postType":  post_type,
                        "updatedAt": now,
                        # Reset createdAt to bring this back to top of feed
                        "createdAt": now,
                    }},
                )
            else:
                await db.notifications.insert_one({
                    "_id":       str(ObjectId()),
                    "toUser":    fun,
                    "fromUser":  poster_un,
                    "type":      "post",
                    "text":      _format_text(poster_un, 1, post_type),
                    "postId":    post_id,
                    "postType":  post_type,
                    "count":     1,
                    "read":      False,
                    "createdAt": now,
                    "updatedAt": now,
                })

    except Exception as e:
        logger.warning("notify_post_to_followers failed: %s", e)
