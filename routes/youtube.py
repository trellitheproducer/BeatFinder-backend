from fastapi import APIRouter, HTTPException, Query
import httpx
import os

router = APIRouter()

YT_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
YT_API_BASE = "https://www.googleapis.com/youtube/v3/search"


@router.get("/search")
async def youtube_search(
    artist: str = Query(...),
    max: int = Query(20, ge=1, le=50),
):
    if not YT_API_KEY:
        raise HTTPException(status_code=500, detail="No API key")

    query = artist + " type beat"
    params = {
        "part": "snippet",
        "type": "video",
        "maxResults": max,
        "q": query,
        "key": YT_API_KEY,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(YT_API_BASE, params=params)
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="YouTube API timed out")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail="YouTube API unreachable")

    if response.status_code != 200:
        data = response.json()
        err = data.get("error", {})
        reason = err.get("errors", [{}])[0].get("reason", "unknown")
        if reason == "keyInvalid":
            raise HTTPException(status_code=401, detail="Invalid YouTube API key")
        if reason == "quotaExceeded":
            raise HTTPException(status_code=429, detail="Quota exceeded")
        if reason == "ipRefererBlocked":
            raise HTTPException(status_code=403, detail="API key restricted")
        raise HTTPException(status_code=response.status_code, detail="YouTube error")

    items = response.json().get("items", [])

    beats = []
    for item in items:
        vid = item.get("id", {}).get("videoId")
        if not vid:
            continue
        snippet = item["snippet"]
        title = snippet["title"]
        title = title.replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")
        channel = snippet["channelTitle"]
        thumbs = snippet.get("thumbnails", {})
        thumb = (
            thumbs.get("high", {}).get("url")
            or thumbs.get("medium", {}).get("url")
            or "https://img.youtube.com/vi/" + vid + "/hqdefault.jpg"
        )
        beats.append({
            "videoId": vid,
            "title": title,
            "channel": channel,
            "thumbnail": thumb,
        })

    return {"query": query, "total": len(beats), "beats": beats}
