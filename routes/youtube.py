from fastapi import APIRouter, HTTPException, Query, Request
from datetime import datetime, timedelta
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

# ── Instrumental filter ───────────────────────────────────────────────────────
# These signals in a title mean it is a song/video, NOT an instrumental beat.
# All matching is case-insensitive (titles are lowercased before checking).

VOCAL_SIGNALS = [
    # English
    "official video", "official music video", "music video", "official mv",
    "official clip", "official audio", "official single", "official hd",
    "lyrics video", "lyric video", "with lyrics", "audio official",
    "visualizer", "visual video",
    "feat.", "ft.", " ft ", " feat ", "featuring",
    "(clean)", "(explicit)", "(dirty)", "(radio edit)",
    "sing along", "karaoke", "cover", "remix ft",
    "out now", "new song", "new single", "new music",
    "official release", "available now", "stream now",
    "listen now", "vevo",
    "dance video", "dance performance", "live performance",
    "behind the scenes", "making of",
    "(mv)", "[mv]", "m/v", "music vid", "musicvideo",
    # Spanish / Portuguese
    "oficial video", "video oficial", "videoclip oficial",
    "vid oficial", "clip oficial", "musica oficial",
    "clipe oficial", "video clipe", "videoclipe",
    # French
    "clip officiel", "video officielle",
]

BEAT_SIGNALS = [
    "type beat", "instrumental", "free beat", "beat free",
    "no copyright", "(free)", "[free]", "rap beat", "trap beat",
    "drill beat", "r&b beat", "afrobeat beat", "melodic beat",
    "free instrumental",
]


def is_instrumental(title: str) -> bool:
    """Return True if the title looks like a genuine beat/instrumental."""
    if not title:
        return True
    t = title.lower()
    # Vocal/video signals always win — reject even if "type beat" also present
    if any(s in t for s in VOCAL_SIGNALS):
        return False
    # Strong beat signal — keep
    if any(s in t for s in BEAT_SIGNALS):
        return True
    # "Artist - Song" pattern with no beat keyword — reject
    if " - " in t and not any(w in t for w in ["beat", "instrumental", "free", "prod"]):
        if any(w in t for w in ["official", "audio", "video", "vevo"]):
            return False
    # Very short title with no beat word — likely a song title
    if not any(w in t for w in ["beat", "instrumental", "free", "prod", "type",
                                  "drill", "trap", "rnb", "afro"]):
        if len(t) < 30:
            return False
    return True


def decode(text):
    text = text.replace("&quot;", QUOTE)
    text = text.replace("&#39;",  APOS)
    text = text.replace("&amp;",  AMP)
    text = text.replace("&lt;",   LT)
    text = text.replace("&gt;",   GT)
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
):
    if not YT_KEY:
        raise HTTPException(status_code=500, detail="No API key configured")

    master_key   = artist.lower().replace(" ", "_") + "_master_v3"
    page_key     = artist.lower().replace(" ", "_") + "_p" + str(page) + "_v4"
    query        = artist + " type beat"
    db           = request.app.state.db

    # 1. Try page-level cache
    cached = await get_cached(db, page_key)
    if cached:
        print("[Cache HIT]  " + page_key)
        return {"query": query, "total": len(cached), "beats": cached, "cached": True}

    # 2. Try master cache
    master = await get_cached(db, master_key)
    if not master:
        print("[Cache MISS] " + master_key + " - fetching from YouTube")
        all_beats    = []
        seen_ids     = set()
        artist_lower = artist.lower()
        fetch_suffixes = ["free", "free instrumental 2024", "free instrumental 2025"]

        async with httpx.AsyncClient(timeout=20.0) as client:
            for suffix in fetch_suffixes:
                try:
                    data = await yt_get(client, YT_SEARCH, {
                        "part":       "snippet",
                        "type":       "video",
                        "maxResults": 50,
                        "q":          artist + " type beat " + suffix,
                        "key":        YT_KEY,
                    })
                    for item in data.get("items", []):
                        vid = item.get("id", {}).get("videoId")
                        if not vid or vid in seen_ids:
                            continue
                        s     = item["snippet"]
                        title = decode(s.get("title", ""))
                        # Filter: artist name must appear (if filter_title on)
                        if filter_title and artist_lower not in title.lower():
                            continue
                        # Filter: must look like an instrumental
                        if not is_instrumental(title):
                            print("[Filter] Rejected: " + title)
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
                    print("[Warn] suffix fetch failed: " + str(e))
                    continue

        master = all_beats
        await set_cached(db, master_key, master)
        print("[Cache SET]  " + master_key + " - " + str(len(master)) + " beats")

    # 3. Slice into pages
    start = (page - 1) * max
    beats = master[start:start + max]
    await set_cached(db, page_key, beats)

    return {"query": query, "total": len(beats), "beats": beats, "cached": False}


# ── Artist photo ──────────────────────────────────────────────────────────────

@router.get("/artist-photo")
async def artist_photo(request: Request, artist: str = Query(...)):
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

    today     = datetime.utcnow().strftime("%Y-%m-%d")
    cache_key = "trending_filtered_" + today
    db        = request.app.state.db

    cached = await get_cached(db, cache_key)
    if cached:
        print("[Cache HIT] trending")
        return {"beats": cached, "cached": True}

    print("[Cache MISS] trending - fetching from YouTube")

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
        s     = item.get("snippet", {})
        title = decode(s.get("title", ""))
        # Apply instrumental filter
        if not is_instrumental(title):
            print("[Filter] Trending rejected: " + title)
            continue
        t = s.get("thumbnails", {})
        beats.append({
            "videoId":    vid,
            "title":      title,
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
    print("[Cache SET] trending - " + str(len(beats)) + " filtered beats")

    return {"beats": beats, "cached": False}
