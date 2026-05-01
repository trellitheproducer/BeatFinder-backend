"""
AI Lyrics Assistant: /api/ai/suggest
Uses Google Gemini API to help users write lyrics.
"""

from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
import httpx
import os

from auth import get_current_user

router = APIRouter()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Try multiple model endpoints in order until one works
GEMINI_MODELS = [
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent",
    "https://generativelanguage.googleapis.com/v1/models/gemini-pro:generateContent",
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.0-pro:generateContent",
]


class SuggestRequest(BaseModel):
    prompt:    str
    lyrics:    Optional[str] = ""
    beatTitle: Optional[str] = ""


@router.post("/suggest")
async def suggest_lyrics(
    body: SuggestRequest,
    request: Request,
    user=Depends(get_current_user),
):
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="AI not configured")

    context_parts = []
    if body.beatTitle:
        context_parts.append("The user is writing lyrics to this beat: \"" + body.beatTitle + "\".")
    if body.lyrics and body.lyrics.strip():
        context_parts.append("Here are the lyrics they have written so far:\n\n" + body.lyrics.strip())
    else:
        context_parts.append("They haven't written any lyrics yet.")
    context_parts.append("User request: " + body.prompt)

    system_prompt = """You are an expert music lyricist and creative writing assistant specialising in rap, R&B, UK drill, grime, afrobeats and melodic trap.

Your job is to help artists write lyrics. Keep responses concise and creative.
- When suggesting lines or verses, format them clearly with line breaks
- Match the energy and style of what the user has already written
- Be creative, authentic and street-aware
- Don't add unnecessary explanation unless asked
- Keep suggestions focused and actionable"""

    full_prompt = system_prompt + "\n\n" + "\n".join(context_parts)

    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {
            "temperature":     0.9,
            "maxOutputTokens": 500,
        },
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        last_error = ""
        for model_url in GEMINI_MODELS:
            url = model_url + "?key=" + GEMINI_API_KEY
            try:
                r = await client.post(url, json=payload)
                print("[Gemini] Tried " + model_url + " -> " + str(r.status_code))
                if r.status_code == 200:
                    data = r.json()
                    try:
                        text = data["candidates"][0]["content"]["parts"][0]["text"]
                        return {"suggestion": text.strip()}
                    except (KeyError, IndexError):
                        print("[Gemini] Unexpected response: " + str(data))
                        last_error = "Unexpected response format"
                        continue
                else:
                    last_error = str(r.status_code) + ": " + r.text
                    print("[Gemini Error] " + last_error)
                    continue
            except Exception as e:
                last_error = str(e)
                print("[Gemini Exception] " + last_error)
                continue

    raise HTTPException(status_code=502, detail="AI unavailable: " + last_error)
