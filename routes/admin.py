"""
Admin routes: /api/admin/
Requires is_admin=True on the user document.
"""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import StreamingResponse
from auth import get_admin_user
import csv
import io

router = APIRouter()


# ── Stats ─────────────────────────────────────────────────────────
@router.get("/stats")
async def stats(request: Request, _=Depends(get_admin_user)):
    db = request.app.state.db

    total_users    = await db.users.count_documents({})
    free_users     = await db.users.count_documents({"plan": "free"})
    artist_pros    = await db.users.count_documents({"plan": "artist"})
    producer_pros  = await db.users.count_documents({"plan": "producer"})
    total_saved    = await db.saved_beats.count_documents({})

    return {
        "total_users":    total_users,
        "free_users":     free_users,
        "artist_pro":     artist_pros,
        "producer_pro":   producer_pros,
        "total_saved_beats": total_saved,
    }


# ── Export email list as CSV ──────────────────────────────────────
@router.get("/export/emails")
async def export_emails(request: Request, _=Depends(get_admin_user)):
    db    = request.app.state.db
    users = await db.users.find(
        {},
        {"name": 1, "email": 1, "plan": 1, "created_at": 1}
    ).to_list(10000)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["name", "email", "plan", "created_at"])
    for u in users:
        writer.writerow([
            u.get("name", ""),
            u.get("email", ""),
            u.get("plan", "free"),
            u.get("created_at", ""),
        ])
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=beatfinder-users.csv"},
    )


# ── List all users ────────────────────────────────────────────────
@router.get("/users")
async def list_users(request: Request, _=Depends(get_admin_user)):
    db    = request.app.state.db
    users = await db.users.find(
        {},
        {"password": 0}     # never return password hashes
    ).sort("created_at", -1).to_list(500)

    return [
        {
            "id":         str(u["_id"]),
            "name":       u.get("name"),
            "email":      u.get("email"),
            "plan":       u.get("plan", "free"),
            "created_at": u.get("created_at", ""),
        }
        for u in users
    ]
