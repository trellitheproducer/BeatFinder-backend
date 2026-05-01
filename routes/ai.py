"""
AI Lyrics Assistant: /api/ai/suggest
Uses Google Gemini 1.5 Flash to help users write lyrics.
"""

from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
import httpx
import os

from auth import get_current_user

router = APIRouter()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_URL     = "https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent"


class SuggestRequest(BaseModel):
    prompt:    str               # what the user asked for
    lyrics:    Optional[str] = ""  # their current lyrics so far
    beatTitle: Optional[str] = ""  # the beat they're writing to


@router.post("/suggest")
async def suggest_lyrics(
    body: SuggestRequest,
    request: Request,
    user=Depends(get_current_user),
):
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="AI not configured")

    # Build a context-aware prompt
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

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            GEMINI_URL + "?key=" + GEMINI_API_KEY,
            json={
                "contents": [
                    {"parts": [{"text": full_prompt}]}
                ],
                "generationConfig": {
                    "temperature":     0.9,
                    "maxOutputTokens": 500,
                },
            },
        )

    if r.status_code != 200:
        print("[Gemini Error] Status: " + str(r.status_code) + " Body: " + r.text)
        raise HTTPException(status_code=502, detail="AI service error: " + r.text)

    data = r.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise HTTPException(status_code=502, detail="Unexpected AI response format")

    return {"suggestion": text.strip()}
