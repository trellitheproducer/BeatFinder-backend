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
        context_parts.append("Beat: \"" + body.beatTitle + "\"")
    if body.lyrics and body.lyrics.strip():
        lines = [l for l in body.lyrics.strip().split("\n") if l.strip()]
        if lines:
            last_line = lines[-1].strip()
            context_parts.append("Their lyrics so far:\n" + body.lyrics.strip())
            context_parts.append("LAST LINE (rhyme with this): \"" + last_line + "\"")
            context_parts.append("Write ONE line that rhymes with: \"" + last_line + "\"")
        else:
            context_parts.append("They haven't written any lyrics yet. Write an opening line.")
    else:
        context_parts.append("They haven't written any lyrics yet. Write a strong opening line.")

    system_prompt = """You are an expert rap and R&B lyricist. Your ONLY job is to suggest the next line that RHYMES with the last line the user wrote.

STRICT RULES:
- Suggest ONE line only — no explanations, no labels, no asterisks, no headers
- The line MUST rhyme with the last line the user wrote
- Match the flow, syllable count and energy of their existing lyrics
- Keep it street, authentic and natural
- Do NOT write "Here's a suggestion:" or any preamble — just the line itself
- Do NOT use asterisks or markdown formatting"""

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
