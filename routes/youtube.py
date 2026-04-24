from fastapi import APIRouter, HTTPException, Query
import httpx
import os

router = APIRouter()

YT_API_KEY   = os.getenv(“YOUTUBE_API_KEY”, “”)
YT_SEARCH    = “https://www.googleapis.com/youtube/v3/search”
YT_CHANNELS  = “https://www.googleapis.com/youtube/v3/channels”

def decode(text: str) -> str:
return (text
.replace(”"”, ‘”’)
.replace(”'”, “’”)
.replace(”&”, “&”)
.replace(”<”, “<”)
.replace(”>”, “>”))

async def yt_get(client: httpx.AsyncClient, url: str, params: dict) -> dict:
try:
r = await client.get(url, params=params)
except httpx.TimeoutException:
raise HTTPException(status_code=504, detail=“YouTube API timed out”)
except httpx.RequestError as e:
raise HTTPException(status_code=502, detail=“YouTube API unreachable”)

```
if r.status_code != 200:
    err    = r.json().get("error", {})
    reason = err.get("errors", [{}])[0].get("reason", "unknown")
    if reason == "keyInvalid":
        raise HTTPException(status_code=401, detail="Invalid YouTube API key")
    if reason == "quotaExceeded":
        raise HTTPException(status_code=429, detail="Quota exceeded. Resets at midnight PT.")
    if reason == "ipRefererBlocked":
        raise HTTPException(status_code=403, detail="API key restricted. Remove HTTP referrer limits.")
    raise HTTPException(status_code=r.status_code, detail=err.get("message", "YouTube error"))

return r.json()
```

# ── Beat search ───────────────────────────────────────────────────

@router.get(”/search”)
async def youtube_search(
artist: str = Query(…),
max:    int = Query(20, ge=1, le=50),
):
if not YT_API_KEY:
raise HTTPException(status_code=500, detail=“YouTube API key not configured”)

```
query = artist + " type beat"

async with httpx.AsyncClient(timeout=10.0) as client:
    data = await yt_get(client, YT_SEARCH, {
        "part":       "snippet",
        "type":       "video",
        "maxResults": max,
        "q":          query,
        "key":        YT_API_KEY,
    })

beats = []
for item in data.get("items", []):
    vid = item.get("id", {}).get("videoId")
    if not vid:
        continue
    s = item["snippet"]
    t = s.get("thumbnails", {})
    beats.append({
        "videoId":   vid,
        "title":     decode(s.get("title", "")),
        "channel":   decode(s.get("channelTitle", "")),
        "thumbnail": (
            t.get("high",   {}).get("url") or
            t.get("medium", {}).get("url") or
            "https://img.youtube.com/vi/" + vid + "/hqdefault.jpg"
        ),
    })

return {"query": query, "total": len(beats), "beats": beats}
```

# - Artist photo - searches YouTube for the artist’s official channel ─

# Returns the channel avatar URL which works for every artist.

@router.get(”/artist-photo”)
async def artist_photo(
artist: str = Query(…),
):
if not YT_API_KEY:
raise HTTPException(status_code=500, detail=“YouTube API key not configured”)

```
async with httpx.AsyncClient(timeout=10.0) as client:
    # Step 1: search for the artist's channel
    search_data = await yt_get(client, YT_SEARCH, {
        "part":       "snippet",
        "type":       "channel",
        "maxResults": 1,
        "q":          artist + " official",
        "key":        YT_API_KEY,
    })

    items = search_data.get("items", [])
    if not items:
        return {"artist": artist, "photo": None}

    channel_id = items[0].get("id", {}).get("channelId")
    if not channel_id:
        return {"artist": artist, "photo": None}

    # Step 2: fetch the channel's avatar thumbnail
    channel_data = await yt_get(client, YT_CHANNELS, {
        "part": "snippet",
        "id":   channel_id,
        "key":  YT_API_KEY,
    })

    channel_items = channel_data.get("items", [])
    if not channel_items:
        return {"artist": artist, "photo": None}

    thumbs = channel_items[0].get("snippet", {}).get("thumbnails", {})
    photo  = (
        thumbs.get("high",   {}).get("url") or
        thumbs.get("medium", {}).get("url") or
        thumbs.get("default",{}).get("url")
    )

return {"artist": artist, "photo": photo}
```
