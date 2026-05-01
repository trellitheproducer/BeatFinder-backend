"""
AI Lyrics Assistant: /api/ai/suggest
Uses Groq API (free, UK compatible) to help users write lyrics.
"""

from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
import httpx
import os

from auth import get_current_user

router = APIRouter()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "openai/gpt-oss-120b"


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
    if not GROQ_API_KEY:
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

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            GROQ_URL,
            headers={
                "Authorization": "Bearer " + GROQ_API_KEY,
                "Content-Type":  "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": "\n".join(context_parts)},
                ],
                "max_tokens":  500,
                "temperature": 0.9,
            },
        )

    print("[Groq] Status: " + str(r.status_code))

    if r.status_code != 200:
        print("[Groq Error] " + r.text[:300])
        raise HTTPException(status_code=502, detail="AI service error: " + r.text[:200])

    data = r.json()
    try:
        text = data["choices"][0]["message"]["content"]
        return {"suggestion": text.strip()}
    except (KeyError, IndexError):
        raise HTTPException(status_code=502, detail="Unexpected AI response format")
