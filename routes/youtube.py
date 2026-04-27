from fastapi import APIRouter, HTTPException, Query, Request
from datetime import datetime, timedelta
from typing import Optional
import httpx
import os

router = APIRouter()

YT_KEY      = os.getenv("YOUTUBE_API_KEY", "")
YT_SEARCH   = "https://www.googleapis.com/youtube/v3/search"
YT_CHANNELS = "https://www.googleapis.com/youtube/v3/channels"
YT_VIDEOS   = "https://www.googleapis.com/youtube/v3/videos"

CACHE_HOURS = 24

QUOTE = chr(34)
APOS  = chr(39)
AMP   = chr(38)
LT    = chr(60)
GT    = chr(62)


def decode(text):
    text = text.replace("&quot;", QUOTE)
    text = text.replace("&#39;", APOS)
    text = text.replace("&amp;", AMP)
    text = text.replace("&lt;", LT)
    text = text.replace("&gt;", GT)
    return text


async def yt_get(client, url, params):
    try:
        r = await client.get(url, params=params)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timed out")
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="Unreachable")
    if r.status_code != 200:
        err    = r.json().get("error", {})
        reason = err.get("errors", [{}])[0].get("reason", "unknown")
        if reason == "keyInvalid":
            raise HTTPException(status_code=401, detail="Invalid API key")
        if reason == "quotaExceeded":
            raise HTTPException(status_code=429, detail="Quota exceeded. Resets at midnight PT.")
        if reason == "ipRefererBlocked":
            raise HTTPException(status_code=403, detail="API key restricted")
        raise HTTPException(status_code=r.status_code, detail="YouTube error")
    return r.json()


# ── MongoDB cache helpers ─────────────────────────────────────────────────────

async def get_cached(db, cache_key):
    doc = await db.yt_cache.find_one({"_id": cache_key})
    if not doc:
        return None
    age = datetime.utcnow() - doc["cached_at"]
    if age > timedelta(hours=CACHE_HOURS):
        return None
    return doc["beats"]


async def set_cached(db, cache_key, beats):
    await db.yt_cache.update_one(
        {"_id": cache_key},
        {"$set": {"beats": beats, "cached_at": datetime.utcnow()}},
        upsert=True,
    )


# ── Beat search ───────────────────────────────────────────────────────────────

@router.get("/search")
async def youtube_search(
    request:      Request,
    artist:       str  = Query(...),
    max:          int  = Query(10, ge=1, le=50),
    page:         int  = Query(1, ge=1, le=10),
    filter_title: bool = Query(True),
    extra_queries: Optional[str] = Query(None),  # comma-separated extra search terms
):
    if not YT_KEY:
        raise HTTPException(status_code=500, detail="No API key configured")

    master_key   = artist.lower().replace(" ", "_") + "_master_v6"
    page_key     = artist.lower().replace(" ", "_") + "_p" + str(page) + "_v6"
    query        = artist + " type beat"

    db = request.app.state.db

    # 1. Try page-level cache
    cached = await get_cached(db, page_key)
    if cached:
        print("[Cache HIT]  " + page_key + " served from MongoDB")
        return {"query": query, "total": len(cached), "beats": cached, "cached": True}

    # 2. Try master cache
    master = await get_cached(db, master_key)
    if not master:
        print("[Cache MISS] " + master_key + " fetching from YouTube")
        all_beats = []
        seen_ids  = set()
        artist_lower = artist.lower()

        # Build fetch queries — extra_queries overrides for specific artists
        if extra_queries:
            fetch_queries = [q.strip() for q in extra_queries.split(",") if q.strip()]
        else:
            # Primary proven queries first, then supplemental expansions
            fetch_queries = [
                artist + " type beat free",
                artist + " type beat free instrumental 2024",
                artist + " type beat free instrumental 2025",
                artist + " type beat",
                artist + " Instrumental",
            ]

        async with httpx.AsyncClient(timeout=20.0) as client:
            for q in fetch_queries:
                try:
                    data = await yt_get(client, YT_SEARCH, {
                        "part":       "snippet",
                        "type":       "video",
                        "maxResults": 50,
                        "q":          q,
                        "key":        YT_KEY,
                    })
                    for item in data.get("items", []):
                        vid = item.get("id", {}).get("videoId")
                        if not vid or vid in seen_ids:
                            continue
                        s     = item["snippet"]
                        title = decode(s.get("title", ""))
                        # filter_title guards against music videos / non-beats
                        if filter_title and artist_lower not in title.lower():
                            continue
                        seen_ids.add(vid)
                        t = s.get("thumbnails", {})
                        all_beats.append({
                            "videoId":   vid,
                            "title":     title,
                            "channel":   decode(s.get("channelTitle", "")),
                            "thumbnail": (
                                t.get("high",   {}).get("url") or
                                t.get("medium", {}).get("url") or
                                "https://img.youtube.com/vi/" + vid + "/hqdefault.jpg"
                            ),
                        })
                except Exception as e:
                    print("[Warn] query fetch failed: " + str(e))
                    continue

        master = all_beats
        await set_cached(db, master_key, master)
        print("[Cache SET]  " + master_key + " - " + str(len(master)) + " total beats")

    # 3. Slice master into pages
    start  = (page - 1) * max
    end    = start + max
    beats  = master[start:end]

    await set_cached(db, page_key, beats)
    print("[Cache SET]  " + page_key + " stored " + str(len(beats)) + " beats")

    return {"query": query, "total": len(beats), "beats": beats, "cached": False}


# ── Artist photo ──────────────────────────────────────────────────────────────

@router.get("/artist-photo")
async def artist_photo(
    request: Request,
    artist:  str = Query(...),
):
    if not YT_KEY:
        raise HTTPException(status_code=500, detail="No API key configured")

    cache_key = "photo_" + artist.lower().replace(" ", "_")
    db        = request.app.state.db

    cached = await get_cached(db, cache_key)
    if cached:
        return {"artist": artist, "photo": cached.get("url")}

    async with httpx.AsyncClient(timeout=10.0) as client:
        search_data = await yt_get(client, YT_SEARCH, {
            "part":       "snippet",
            "type":       "channel",
            "maxResults": 1,
            "q":          artist + " official",
            "key":        YT_KEY,
        })
        items = search_data.get("items", [])
        if not items:
            return {"artist": artist, "photo": None}

        channel_id = items[0].get("id", {}).get("channelId")
        if not channel_id:
            return {"artist": artist, "photo": None}

        channel_data = await yt_get(client, YT_CHANNELS, {
            "part": "snippet",
            "id":   channel_id,
            "key":  YT_KEY,
        })
        channel_items = channel_data.get("items", [])
        if not channel_items:
            return {"artist": artist, "photo": None}

        thumbs = channel_items[0].get("snippet", {}).get("thumbnails", {})
        photo  = (
            thumbs.get("high",    {}).get("url") or
            thumbs.get("medium",  {}).get("url") or
            thumbs.get("default", {}).get("url")
        )

    await set_cached(db, cache_key, {"url": photo})
    return {"artist": artist, "photo": photo}


# ── Cache stats ───────────────────────────────────────────────────────────────

@router.get("/cache-stats")
async def cache_stats(request: Request):
    db    = request.app.state.db
    total = await db.yt_cache.count_documents({})
    fresh = await db.yt_cache.count_documents({
        "cached_at": {"$gte": datetime.utcnow() - timedelta(hours=CACHE_HOURS)}
    })
    return {
        "total_cached_queries": total,
        "fresh_entries":        fresh,
        "cache_ttl_hours":      CACHE_HOURS,
        "message":              "Each fresh entry = 0 YouTube API calls saved",
    }


# ── Trending beats ────────────────────────────────────────────────────────────

def format_views(n):
    if n >= 1_000_000:
        return str(round(n / 1_000_000, 1)) + "M views"
    if n >= 1_000:
        return str(round(n / 1_000, 1)) + "K views"
    return str(n) + " views"


@router.get("/trending")
async def trending_beats(request: Request):
    if not YT_KEY:
        raise HTTPException(status_code=500, detail="No API key configured")

    cache_key = "trending_1m_v2"
    db = request.app.state.db

    cached = await get_cached(db, cache_key)
    if cached:
        print("[Cache HIT] trending_1m")
        return {"beats": cached, "cached": True}

    print("[Cache MISS] trending_1m - fetching from YouTube")

    async with httpx.AsyncClient(timeout=15.0) as client:
        search_data = await yt_get(client, YT_SEARCH, {
            "part":       "snippet",
            "type":       "video",
            "maxResults": 50,
            "q":          "type beat free 2025",
            "order":      "viewCount",
            "key":        YT_KEY,
        })

        items = search_data.get("items", [])
        if not items:
            return {"beats": [], "cached": False}

        video_ids = [i["id"]["videoId"] for i in items if i.get("id", {}).get("videoId")]

        stats_data = await yt_get(client, YT_VIDEOS, {
            "part": "statistics,snippet",
            "id":   ",".join(video_ids),
            "key":  YT_KEY,
        })

    beats = []
    for item in stats_data.get("items", []):
        vid = item.get("id")
        if not vid:
            continue
        stats = item.get("statistics", {})
        views = int(stats.get("viewCount", 0))
        if views < 1_000_000:
            continue
        s = item.get("snippet", {})
        t = s.get("thumbnails", {})
        beats.append({
            "videoId":    vid,
            "title":      decode(s.get("title", "")),
            "channel":    decode(s.get("channelTitle", "")),
            "thumbnail":  (
                t.get("high",   {}).get("url") or
                t.get("medium", {}).get("url") or
                "https://img.youtube.com/vi/" + vid + "/hqdefault.jpg"
            ),
            "views":      views,
            "viewsLabel": format_views(views),
        })

    beats.sort(key=lambda b: b["views"], reverse=True)
    beats = beats[:10]

    await set_cached(db, cache_key, beats)
    print("[Cache SET] trending_1m - " + str(len(beats)) + " beats with 1M+ views")

    return {"beats": beats, "cached": False}
