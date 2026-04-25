from fastapi import APIRouter, HTTPException, Query
import httpx
import os

router = APIRouter()

YT_KEY = os.getenv("YOUTUBE_API_KEY", "")
YT_SEARCH = "https://www.googleapis.com/youtube/v3/search"
YT_CHANNELS = "https://www.googleapis.com/youtube/v3/channels"

QUOTE = chr(34)
APOS = chr(39)
AMP = chr(38)
LT = chr(60)
GT = chr(62)


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
        err = r.json().get("error", {})
        reason = err.get("errors", [{}])[0].get("reason", "unknown")
        if reason == "keyInvalid":
            raise HTTPException(status_code=401, detail="Invalid API key")
        if reason == "quotaExceeded":
            raise HTTPException(status_code=429, detail="Quota exceeded")
        if reason == "ipRefererBlocked":
            raise HTTPException(status_code=403, detail="API key restricted")
        raise HTTPException(status_code=r.status_code, detail="YouTube error")
    return r.json()


@router.get("/search")
async def youtube_search(
    artist: str = Query(...),
    max: int = Query(20, ge=1, le=50),
    page: int = Query(1, ge=1, le=10),
):
    if not YT_KEY:
        raise HTTPException(status_code=500, detail="No API key")
    suffix = PAGE_SUFFIXES[(page - 1) % len(PAGE_SUFFIXES)]
    query = artist + " type beat " + suffix
    async with httpx.AsyncClient(timeout=10.0) as client:
        data = await yt_get(client, YT_SEARCH, {
            "part": "snippet",
            "type": "video",
            "maxResults": max,
            "q": query,
            "key": YT_KEY,
        })
    artist_lower = artist.lower()
    beats = []
    for item in data.get("items", []):
        vid = item.get("id", {}).get("videoId")
        if not vid:
            continue
        s = item["snippet"]
        title = decode(s.get("title", ""))
        # Only include beats where the artist name appears in the title
        if artist_lower not in title.lower():
            continue
        t = s.get("thumbnails", {})
        beats.append({
            "videoId": vid,
            "title": title,
            "channel": decode(s.get("channelTitle", "")),
            "thumbnail": (
                t.get("high", {}).get("url") or
                t.get("medium", {}).get("url") or
                "https://img.youtube.com/vi/" + vid + "/hqdefault.jpg"
            ),
        })
    return {"query": query, "total": len(beats), "beats": beats}


@router.get("/artist-photo")
async def artist_photo(artist: str = Query(...)):
    if not YT_KEY:
        raise HTTPException(status_code=500, detail="No API key")
    async with httpx.AsyncClient(timeout=10.0) as client:
        search_data = await yt_get(client, YT_SEARCH, {
            "part": "snippet",
            "type": "channel",
            "maxResults": 1,
            "q": artist + " official",
            "key": YT_KEY,
        })
        items = search_data.get("items", [])
        if not items:
            return {"artist": artist, "photo": None}
        channel_id = items[0].get("id", {}).get("channelId")
        if not channel_id:
            return {"artist": artist, "photo": None}
        channel_data = await yt_get(client, YT_CHANNELS, {
            "part": "snippet",
            "id": channel_id,
            "key": YT_KEY,
        })
        channel_items = channel_data.get("items", [])
        if not channel_items:
            return {"artist": artist, "photo": None}
        thumbs = channel_items[0].get("snippet", {}).get("thumbnails", {})
        photo = (
            thumbs.get("high", {}).get("url") or
            thumbs.get("medium", {}).get("url") or
            thumbs.get("default", {}).get("url")
        )
    return {"artist": artist, "photo": photo}


PAGE_SUFFIXES = [
    "free instrumental",
    "2025 free",
    "2024 free instrumental",
    "new 2025",
    "hard trap instrumental",
    "melodic instrumental",
    "free download",
    "best free",
    "official instrumental",
    "2026 free instrumental",
]
