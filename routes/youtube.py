“””
YouTube proxy: /api/youtube/search?artist=Drake&max=20

This is the KEY route that fixes the CORS issue.
The browser calls THIS backend (same origin / CORS allowed).
THIS backend calls googleapis.com server-to-server (no CORS).
Results are returned directly to the frontend.
“””

from fastapi import APIRouter, Request, HTTPException, Query
import httpx
import os

router = APIRouter()

YT_API_KEY  = os.getenv(“YOUTUBE_API_KEY”, “”)
YT_API_BASE = “https://www.googleapis.com/youtube/v3/search”

@router.get(”/search”)
async def youtube_search(
artist:  str = Query(…, description=“Artist name, e.g. Drake”),
max:     int = Query(20, ge=1, le=50, description=“Number of results”),
):
if not YT_API_KEY:
raise HTTPException(status_code=500, detail=“YouTube API key not configured on server”)

```
query = f"{artist} type beat"
params = {
    "part":       "snippet",
    "type":       "video",
    "maxResults": max,
    "q":          query,
    "key":        YT_API_KEY,
}

async with httpx.AsyncClient(timeout=10.0) as client:
    try:
        response = await client.get(YT_API_BASE, params=params)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="YouTube API timed out")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"YouTube API unreachable: {e}")

if response.status_code != 200:
    data = response.json()
    err  = data.get("error", {})
    reason  = err.get("errors", [{}])[0].get("reason", "unknown")
    message = err.get("message", "YouTube API error")

    status_map = {
        "keyInvalid":            (401, "Invalid YouTube API key"),
        "quotaExceeded":         (429, "YouTube API quota exceeded. Try again after midnight PT."),
        "ipRefererBlocked":      (403, "API key restricted. Remove HTTP referrer restrictions."),
    }
    code, detail = status_map.get(reason, (response.status_code, message))
    raise HTTPException(status_code=code, detail=detail)

data  = response.json()
items = data.get("items", [])

# Map: every field from the SAME API item — they always match
def decode(text: str) -> str:
    return (text
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">"))

beats = [
    {
        "videoId":   item["id"]["videoId"],
        "title":     decode(item["snippet"]["title"]),
        "channel":   decode(item["snippet"]["channelTitle"]),
        "thumbnail": (
            item["snippet"]["thumbnails"].get("high", {}).get("url")
            or item["snippet"]["thumbnails"].get("medium", {}).get("url")
            or f"https://img.youtube.com/vi/{item['id']['videoId']}/hqdefault.jpg"
        ),
    }
    for item in items
    if item.get("id", {}).get("videoId")
]

return {
    "query":       query,
    "total":       len(beats),
    "beats":       beats,
}
```
