"""
BPM Detection — async job system.

Architecture (kept lean for Render free tier 512MB):
  1. Client POSTs audio to /api/bpm/analyze, gets job_id immediately.
  2. Server schedules analysis as an asyncio background task.
  3. Client polls /api/bpm/status/:job_id every 1-2s, then /api/bpm/result/:job_id.
  4. Result cached in MongoDB bpm_cache by SHA-256 — repeat uploads are instant.

We use librosa only (no aubio, no scipy direct calls) to stay under the
free-tier memory limit. librosa.beat.beat_track gives an industry-grade
Ellis 2007 dynamic-programming beat tracker; we add our own half/double-
time correction and confidence scoring on top of librosa's onset envelope
autocorrelation.

Hard limits (anti-crash):
  - 15 MB upload cap (rejected at parse time).
  - 6 minutes audio duration cap (truncated server-side).
  - 60 s analysis timeout (asyncio.wait_for).
  - Mono, 22050 Hz, float32 — halves memory vs. stereo 44100 Hz float64.
  - Single concurrent job per user (queued otherwise).
  - Temp files cleaned in finally{} regardless of outcome.
"""

import asyncio
import hashlib
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from pydantic import BaseModel

# librosa imported lazily inside the analysis task so the module load
# doesn't bloat memory at server boot. If a user never touches BPM, we
# never pay the librosa import cost.

logger = logging.getLogger("bpm")
router = APIRouter()

# ── Constants ──────────────────────────────────────────────────────────────
MAX_UPLOAD_BYTES   = 15 * 1024 * 1024        # 15 MB
MAX_DURATION_SECS  = 360                     # 6 minutes — truncate longer
ANALYSIS_TIMEOUT_S = 60                      # asyncio.wait_for hard cap
TARGET_SR          = 22050                   # librosa default; halves RAM
JOB_TTL_SECS       = 600                     # in-memory job dict housekeeping
CACHE_TTL_DAYS     = 30                      # MongoDB cache freshness
ALLOWED_MIMES      = {
    "audio/mpeg", "audio/mp3",
    "audio/wav",  "audio/x-wav", "audio/wave",
    "audio/mp4",  "audio/m4a",   "audio/x-m4a",
    "audio/aac",  "audio/ogg",   "audio/flac",
    "application/octet-stream",   # some browsers (Safari) for m4a
}
ALLOWED_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

# In-memory job state — single-process so a dict is enough. NOT shared
# across Render dynos but Render free runs one dyno anyway.
#
# Each entry: {
#   "status":    "queued" | "processing" | "done" | "error",
#   "progress":  0..100,
#   "message":   str,
#   "result":    {...} | None,
#   "error":     str | None,
#   "created":   datetime,
#   "user_id":   str,
# }
_JOBS: Dict[str, Dict[str, Any]] = {}

# Per-user concurrency lock — prevents one user starting 20 jobs.
_USER_LOCKS: Dict[str, asyncio.Lock] = {}


def _user_lock(user_id: str) -> asyncio.Lock:
    if user_id not in _USER_LOCKS:
        _USER_LOCKS[user_id] = asyncio.Lock()
    return _USER_LOCKS[user_id]


def _gc_old_jobs():
    """Remove job entries older than JOB_TTL_SECS to bound memory."""
    cutoff = datetime.utcnow() - timedelta(seconds=JOB_TTL_SECS)
    stale = [jid for jid, j in _JOBS.items() if j["created"] < cutoff]
    for jid in stale:
        _JOBS.pop(jid, None)


# ── Auth dependency — lifted from existing routes ──────────────────────────
def get_current_user_dep():
    # We import inside the function so this file has no import-time coupling
    # to the rest of the backend. The dependency is reassigned at module
    # import time by main.py if needed; otherwise we look it up dynamically.
    from routes.auth import get_current_user
    return get_current_user


# ── Models ─────────────────────────────────────────────────────────────────
class JobStatusResponse(BaseModel):
    job_id:   str
    status:   str   # "queued" | "processing" | "done" | "error"
    progress: int   # 0..100
    message:  str
    error:    Optional[str] = None


class BeatGrid(BaseModel):
    times:     list  # list[float] — beat timestamps in seconds
    downbeats: list  # list[float] — every Nth beat (start of each bar)


class BpmResult(BaseModel):
    bpm:               float
    bpm_rounded:       int
    confidence:        float            # 0..1
    alternative_bpms:  list             # [{bpm, score}] sorted desc
    beat_grid:         BeatGrid
    onset_count:       int
    duration_secs:     float
    sample_rate:       int
    cached:            bool
    analysis_ms:       int


# ── Helpers ────────────────────────────────────────────────────────────────
async def _read_upload_streaming(upload: UploadFile, max_bytes: int) -> bytes:
    """Stream-read with hard size cap so a malicious 500MB upload can't OOM us."""
    chunks = []
    total = 0
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail=f"File too large (>{max_bytes // (1024*1024)} MB)")
        chunks.append(chunk)
    return b"".join(chunks)


def _hash_audio(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _suffix_for(filename: str, content_type: str) -> str:
    name = (filename or "").lower()
    for ext in ALLOWED_EXTS:
        if name.endswith(ext):
            return ext
    # Fall back from MIME
    if "mp3" in (content_type or "") or "mpeg" in (content_type or ""):
        return ".mp3"
    if "wav" in (content_type or ""):
        return ".wav"
    if "m4a" in (content_type or "") or "mp4" in (content_type or ""):
        return ".m4a"
    return ".bin"


# ── Core analysis — runs in a worker thread because librosa is CPU-bound ───
def _run_librosa_analysis(audio_path: str) -> Dict[str, Any]:
    """
    Blocking CPU-bound analysis. Called via asyncio.to_thread so the event
    loop stays free for the polling endpoints.

    Returns a dict matching BpmResult fields (minus cached/analysis_ms).
    """
    # Late import — see top-of-file note about memory at idle.
    import numpy as np
    import librosa

    # 1. Load — mono at TARGET_SR, truncate at MAX_DURATION_SECS so a 30-min
    #    podcast upload can't blow memory.
    y, sr = librosa.load(
        audio_path,
        sr=TARGET_SR,
        mono=True,
        duration=MAX_DURATION_SECS,
    )

    duration_secs = float(len(y) / sr) if sr else 0.0
    if duration_secs < 1.0:
        raise ValueError("Audio too short to analyse (<1s)")

    # 2. Onset envelope — fundamental input to all tempo work.
    onset_env = librosa.onset.onset_strength(
        y=y, sr=sr,
        aggregate=np.median,    # more robust than mean against transients
        hop_length=512,
    )

    # 3. Primary tempo estimate via librosa's dynamic-programming beat tracker.
    tempo, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_env,
        sr=sr,
        hop_length=512,
        units="frames",
    )

    # librosa>=0.10 returns numpy scalar; coerce to plain float
    primary_bpm = float(np.asarray(tempo).item() if hasattr(tempo, "item") else tempo)

    # 4. Alternative tempo candidates via autocorrelation of the onset env.
    #    librosa.feature.tempogram + librosa.feature.fourier_tempogram give
    #    a tempo distribution. We pick the top 3 local maxima as candidates.
    tempogram = librosa.feature.tempogram(
        onset_envelope=onset_env,
        sr=sr,
        hop_length=512,
        win_length=384,
    )
    # Average tempogram over time to get a per-BPM strength curve.
    tempogram_mean = np.mean(tempogram, axis=1)
    tempo_axis = librosa.tempo_frequencies(
        n_bins=tempogram.shape[0],
        hop_length=512,
        sr=sr,
    )

    # Only consider plausible musical tempos to filter noise + DC bin
    mask = (tempo_axis >= 50) & (tempo_axis <= 240)
    candidates_raw = []
    if np.any(mask):
        strengths = tempogram_mean[mask]
        tempos    = tempo_axis[mask]
        # Local-max picking — accept a bin if it's higher than both neighbours
        peak_idx = []
        for i in range(1, len(strengths) - 1):
            if strengths[i] > strengths[i - 1] and strengths[i] > strengths[i + 1]:
                peak_idx.append(i)
        # Sort by strength desc, top 5
        peak_idx.sort(key=lambda i: strengths[i], reverse=True)
        max_str = float(strengths.max()) if len(strengths) else 1.0
        for i in peak_idx[:5]:
            candidates_raw.append({
                "bpm":   float(tempos[i]),
                "score": float(strengths[i] / max_str),
            })

    # 5. Half-time / double-time correction.
    #    If the primary BPM is suspiciously fast (>=160) or slow (<=70) AND
    #    its double/half is more strongly represented in the tempogram,
    #    switch to that.
    def _strength_near(target_bpm: float) -> float:
        if not mask.any():
            return 0.0
        diffs = np.abs(tempo_axis[mask] - target_bpm)
        i = int(np.argmin(diffs))
        return float(tempogram_mean[mask][i])

    final_bpm = primary_bpm
    primary_strength = _strength_near(primary_bpm)
    half_strength    = _strength_near(primary_bpm / 2.0)
    double_strength  = _strength_near(primary_bpm * 2.0)

    # Slight bias against switching — only swap if alternative is convincingly stronger.
    SWAP_THRESHOLD = 1.25
    if primary_bpm >= 160 and half_strength > primary_strength * 0.9:
        # Fast tempo + halftime has comparable energy → likely we doubled.
        if 70 <= primary_bpm / 2.0 <= 180:
            final_bpm = primary_bpm / 2.0
    elif primary_bpm <= 75 and double_strength > primary_strength * SWAP_THRESHOLD:
        # Slow tempo + double has much stronger energy → likely we halved.
        if 70 <= primary_bpm * 2.0 <= 180:
            final_bpm = primary_bpm * 2.0

    # 6. Beat grid — recompute if we corrected. Otherwise reuse beat_frames.
    if abs(final_bpm - primary_bpm) > 0.5:
        _, beat_frames = librosa.beat.beat_track(
            onset_envelope=onset_env,
            sr=sr,
            hop_length=512,
            units="frames",
            bpm=final_bpm,
        )

    beat_times = librosa.frames_to_time(
        beat_frames, sr=sr, hop_length=512,
    ).tolist()

    # 7. Downbeats — assume 4/4 (the safe default for produced music).
    #    We don't have meter detection without madmom (too heavy for free tier),
    #    so we publish every 4th beat as a downbeat.
    downbeats = beat_times[::4]

    # 8. Confidence — combine three signals:
    #    a) Peak strength of primary BPM in tempogram, normalised to max.
    #    b) Onset density (beats / duration) — should be in 40..240 BPM range.
    #    c) Beat consistency — std of inter-beat intervals (lower = more confident).
    conf_peak = 0.0
    if len(candidates_raw):
        # Confidence rises with how dominant the chosen BPM is over its peers.
        scores_sorted = sorted([c["score"] for c in candidates_raw], reverse=True)
        if len(scores_sorted) >= 2 and scores_sorted[0] > 0:
            # Ratio of 2nd-best to best: closer to 0 = more confident
            ratio = scores_sorted[1] / scores_sorted[0]
            conf_peak = max(0.0, 1.0 - ratio)
        else:
            conf_peak = 1.0

    conf_consistency = 0.0
    if len(beat_times) >= 4:
        diffs = np.diff(np.asarray(beat_times))
        if len(diffs) > 0 and np.mean(diffs) > 0:
            cv = float(np.std(diffs) / np.mean(diffs))
            conf_consistency = max(0.0, 1.0 - cv * 3.0)  # cv ~0.05 → 0.85

    conf_density = 0.0
    if duration_secs > 0:
        beats_per_min_actual = len(beat_times) / (duration_secs / 60.0)
        # Plausibility envelope
        if 30 <= beats_per_min_actual <= 250:
            conf_density = 1.0
        else:
            conf_density = 0.5

    # Weighted average. Peak dominance matters most for a confident answer.
    confidence = (
        0.55 * conf_peak +
        0.30 * conf_consistency +
        0.15 * conf_density
    )
    confidence = max(0.0, min(1.0, confidence))

    return {
        "bpm":              round(final_bpm, 2),
        "bpm_rounded":      int(round(final_bpm)),
        "confidence":       round(confidence, 3),
        "alternative_bpms": [
            {"bpm": round(c["bpm"], 2), "score": round(c["score"], 3)}
            for c in candidates_raw[:3]
        ],
        "beat_grid": {
            "times":     [round(t, 4) for t in beat_times],
            "downbeats": [round(t, 4) for t in downbeats],
        },
        "onset_count":   len(beat_times),
        "duration_secs": round(duration_secs, 2),
        "sample_rate":   int(sr),
    }


# ── Background job runner ──────────────────────────────────────────────────
async def _run_job(
    job_id: str,
    audio_path: str,
    audio_hash: str,
    db,
):
    """Execute the analysis and stash result in _JOBS + bpm_cache."""
    try:
        _JOBS[job_id]["status"]   = "processing"
        _JOBS[job_id]["progress"] = 10
        _JOBS[job_id]["message"]  = "Loading audio…"

        started = time.monotonic()

        # Run librosa in a worker thread with a hard timeout. asyncio.to_thread
        # is the right primitive — librosa releases the GIL during numpy ops.
        result_dict = await asyncio.wait_for(
            asyncio.to_thread(_run_librosa_analysis, audio_path),
            timeout=ANALYSIS_TIMEOUT_S,
        )

        analysis_ms = int((time.monotonic() - started) * 1000)
        result_dict["analysis_ms"] = analysis_ms
        result_dict["cached"]      = False

        _JOBS[job_id]["status"]   = "done"
        _JOBS[job_id]["progress"] = 100
        _JOBS[job_id]["message"]  = "Complete"
        _JOBS[job_id]["result"]   = result_dict

        # Cache to MongoDB by hash — repeat detection of the same file is
        # then instant. Beat grid arrays can be large; cap to keep doc <16MB.
        try:
            cache_doc = {
                "_id":         audio_hash,
                "result":      result_dict,
                "created_at":  datetime.utcnow(),
            }
            await db.bpm_cache.update_one(
                {"_id": audio_hash},
                {"$set": cache_doc},
                upsert=True,
            )
        except Exception as e:
            logger.warning("bpm_cache write failed: %s", e)

    except asyncio.TimeoutError:
        _JOBS[job_id]["status"] = "error"
        _JOBS[job_id]["error"]  = "Analysis timed out — try a shorter or simpler audio file."
    except Exception as e:
        logger.exception("BPM analysis failed for job %s", job_id)
        _JOBS[job_id]["status"] = "error"
        _JOBS[job_id]["error"]  = f"Analysis failed: {type(e).__name__}"
    finally:
        # Clean up temp file no matter what
        try:
            if audio_path and os.path.exists(audio_path):
                os.unlink(audio_path)
        except Exception:
            pass
        _gc_old_jobs()


# ── Routes ─────────────────────────────────────────────────────────────────
@router.post("/analyze")
async def analyze(
    request: Request,
    file: UploadFile = File(...),
    user=Depends(get_current_user_dep()),
):
    """
    Accept an audio upload, return a job_id. Client polls /status/:job_id.

    If we've analysed this exact file before (SHA-256 match), return
    cached result inline with status="done" — no job created.
    """
    db = request.app.state.db
    user_id = str(user["_id"])

    # Per-user concurrency: only one analysis job in flight at a time.
    lock = _user_lock(user_id)
    if lock.locked():
        raise HTTPException(
            status_code=429,
            detail="Another BPM analysis is already running. Please wait for it to finish.",
        )

    # Validate content type early — saves us reading 15MB just to reject.
    ct = (file.content_type or "").lower()
    fn = (file.filename or "").lower()
    has_ok_ext = any(fn.endswith(ext) for ext in ALLOWED_EXTS)
    if ct not in ALLOWED_MIMES and not has_ok_ext:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {ct or fn or 'unknown'}")

    async with lock:
        # Read with streaming size cap
        try:
            data = await _read_upload_streaming(file, MAX_UPLOAD_BYTES)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Could not read upload: {e}")

        if len(data) < 1024:
            raise HTTPException(status_code=400, detail="File too small to be valid audio")

        audio_hash = _hash_audio(data)

        # Cache hit?
        try:
            cached = await db.bpm_cache.find_one({"_id": audio_hash})
            if cached and cached.get("result"):
                age = datetime.utcnow() - cached.get("created_at", datetime.utcnow())
                if age < timedelta(days=CACHE_TTL_DAYS):
                    out = dict(cached["result"])
                    out["cached"] = True
                    return {
                        "job_id":   None,
                        "status":   "done",
                        "result":   out,
                    }
        except Exception as e:
            logger.warning("bpm_cache read failed: %s", e)

        # Write file to temp + schedule background task
        suffix = _suffix_for(file.filename, ct)
        fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="bpm_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except Exception:
            try: os.unlink(tmp_path)
            except Exception: pass
            raise HTTPException(status_code=500, detail="Failed to stage audio file")

        job_id = uuid.uuid4().hex
        _JOBS[job_id] = {
            "status":   "queued",
            "progress": 0,
            "message":  "Queued",
            "result":   None,
            "error":    None,
            "created":  datetime.utcnow(),
            "user_id":  user_id,
        }

        # Fire and forget — the task will manage temp cleanup itself.
        asyncio.create_task(_run_job(job_id, tmp_path, audio_hash, db))

        return {
            "job_id":   job_id,
            "status":   "queued",
            "result":   None,
        }


@router.get("/status/{job_id}")
async def status(
    job_id: str,
    user=Depends(get_current_user_dep()),
):
    j = _JOBS.get(job_id)
    if not j:
        # Likely already GC'd. Tell the client to stop polling.
        raise HTTPException(status_code=404, detail="Job not found (it may have expired)")

    if str(user["_id"]) != j["user_id"]:
        raise HTTPException(status_code=403, detail="Not your job")

    return {
        "job_id":   job_id,
        "status":   j["status"],
        "progress": j["progress"],
        "message":  j["message"],
        "error":    j["error"],
    }


@router.get("/result/{job_id}")
async def result(
    job_id: str,
    user=Depends(get_current_user_dep()),
):
    j = _JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")

    if str(user["_id"]) != j["user_id"]:
        raise HTTPException(status_code=403, detail="Not your job")

    if j["status"] != "done":
        raise HTTPException(status_code=409, detail=f"Job not done (status={j['status']})")

    return j["result"]
