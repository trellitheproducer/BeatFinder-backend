"""
Contract PDF generation: /api/contracts
Generates polished PDF contracts for free downloads and paid leases.

iOS Safari treats Content-Disposition: attachment + application/pdf as a real
PDF file, so the share sheet shows "Save to Files" immediately without the
user having to switch from Web Archive to PDF in the Options menu.
"""

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from io import BytesIO
import re as _re

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether,
)
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from auth import get_current_user

router = APIRouter()

# ── Brand colours ────────────────────────────────────────────────────────────
BF_PURPLE = colors.HexColor("#9333EA")
BF_AMBER  = colors.HexColor("#F59E0B")
BF_BLACK  = colors.HexColor("#0A0A0A")
BF_INK    = colors.HexColor("#1F1F1F")
BF_GREY   = colors.HexColor("#6B7280")
BF_RULE   = colors.HexColor("#E5E7EB")
BF_BG     = colors.HexColor("#FAFAFA")


def _safe_filename(s: str, max_len: int = 60) -> str:
    s = _re.sub(r'[^\w\s\-]', '', s or "").strip().replace(" ", "_")
    return (s or "Contract")[:max_len]


def _gbp_date(dt) -> str:
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            return dt
    if not dt:
        dt = datetime.utcnow()
    return dt.strftime("%d %B %Y")


# ── Page chrome: header on every page + footer with page numbers ─────────────
def _draw_chrome(c: canvas.Canvas, doc, *, kind: str, ref: str):
    """kind = 'FREE' or 'LEASE' — drawn for every page."""
    width, height = A4
    accent = BF_PURPLE if kind == "FREE" else BF_AMBER

    # Header band
    c.saveState()
    c.setFillColor(BF_BLACK)
    c.rect(0, height - 22 * mm, width, 22 * mm, fill=1, stroke=0)

    # Wordmark
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(20 * mm, height - 13 * mm, "BEAT")
    c.setFillColor(accent)
    text_width = c.stringWidth("BEAT", "Helvetica-Bold", 16)
    c.drawString(20 * mm + text_width, height - 13 * mm, "FINDER")

    # Tagline
    c.setFillColor(colors.HexColor("#9CA3AF"))
    c.setFont("Helvetica", 7)
    c.drawString(20 * mm, height - 18 * mm, "BEATFINDER.CO.UK")

    # Type badge top-right
    badge_w = 38 * mm
    badge_h = 7 * mm
    badge_x = width - 20 * mm - badge_w
    badge_y = height - 15 * mm
    c.setStrokeColor(accent)
    c.setFillColor(accent)
    c.roundRect(badge_x, badge_y, badge_w, badge_h, 3, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 8)
    label = "FREE LICENCE" if kind == "FREE" else "PAID LEASE"
    c.drawCentredString(badge_x + badge_w / 2, badge_y + 2 * mm, label)

    # Accent rule under header
    c.setFillColor(accent)
    c.rect(0, height - 23 * mm, width, 0.6 * mm, fill=1, stroke=0)

    # Footer
    c.setFillColor(BF_GREY)
    c.setFont("Helvetica", 7)
    c.drawString(20 * mm, 12 * mm, "Ref: " + (ref or ""))
    c.drawCentredString(width / 2, 12 * mm,
                        "© " + str(datetime.utcnow().year) + " BeatFinder — beatfinder.co.uk")
    c.drawRightString(width - 20 * mm, 12 * mm, "Page " + str(doc.page))

    # Subtle watermark for FREE — discourages misuse
    if kind == "FREE":
        c.saveState()
        c.setFont("Helvetica-Bold", 60)
        c.setFillColor(colors.HexColor("#F5F0FF"))
        c.translate(width / 2, height / 2)
        c.rotate(35)
        c.drawCentredString(0, 0, "NON-COMMERCIAL")
        c.restoreState()

    c.restoreState()


def _styles():
    base = getSampleStyleSheet()
    s = {}
    s["title"] = ParagraphStyle(
        "title", parent=base["Title"], fontName="Helvetica-Bold",
        fontSize=18, leading=22, alignment=TA_CENTER, textColor=BF_BLACK,
        spaceAfter=4,
    )
    s["subtitle"] = ParagraphStyle(
        "subtitle", parent=base["Normal"], fontName="Helvetica",
        fontSize=9, leading=12, alignment=TA_CENTER, textColor=BF_GREY,
        spaceAfter=14,
    )
    s["h2"] = ParagraphStyle(
        "h2", parent=base["Heading2"], fontName="Helvetica-Bold",
        fontSize=10.5, leading=14, textColor=BF_BLACK,
        spaceBefore=12, spaceAfter=4, letterSpace=0.6,
    )
    s["body"] = ParagraphStyle(
        "body", parent=base["Normal"], fontName="Helvetica",
        fontSize=10, leading=15, textColor=BF_INK,
        alignment=TA_JUSTIFY, spaceAfter=6,
    )
    s["small"] = ParagraphStyle(
        "small", parent=base["Normal"], fontName="Helvetica",
        fontSize=8.5, leading=12, textColor=BF_GREY, alignment=TA_CENTER,
    )
    s["sig_label"] = ParagraphStyle(
        "sig_label", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=8, leading=11, textColor=BF_GREY, letterSpace=0.8,
    )
    s["sig_value"] = ParagraphStyle(
        "sig_value", parent=base["Normal"], fontName="Helvetica",
        fontSize=10.5, leading=14, textColor=BF_BLACK,
    )
    return s


def _info_table(rows):
    t = Table(rows, colWidths=[35 * mm, 130 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), BF_BG),
        ("BOX",          (0, 0), (-1, -1), 0.5, BF_RULE),
        ("INNERGRID",    (0, 0), (-1, -1), 0.25, BF_RULE),
        ("FONT",         (0, 0), (0, -1), "Helvetica-Bold", 9),
        ("FONT",         (1, 0), (1, -1), "Helvetica", 9.5),
        ("TEXTCOLOR",    (0, 0), (0, -1), BF_GREY),
        ("TEXTCOLOR",    (1, 0), (1, -1), BF_BLACK),
        ("LEFTPADDING",  (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING",   (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 7),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def _signature_block(licensee_name: str, licensee_email: str, ref: str, date_str: str):
    rows = [
        [Paragraph("LICENSEE", _styles()["sig_label"]),
         Paragraph("DATE", _styles()["sig_label"])],
        [Paragraph(f'<b>{licensee_name}</b><br/>'
                   f'<font size="8" color="#6B7280">{licensee_email}</font>',
                   _styles()["sig_value"]),
         Paragraph(date_str, _styles()["sig_value"])],
        [Paragraph("REFERENCE", _styles()["sig_label"]),
         Paragraph("ACCEPTANCE", _styles()["sig_label"])],
        [Paragraph(f'<font size="9" color="#1F1F1F">{ref}</font>', _styles()["sig_value"]),
         Paragraph('<b>Accepted electronically</b><br/>'
                   '<font size="8" color="#6B7280">via BeatFinder platform</font>',
                   _styles()["sig_value"])],
    ]
    t = Table(rows, colWidths=[82 * mm, 82 * mm])
    t.setStyle(TableStyle([
        ("BOX",           (0, 0), (-1, -1), 0.5, BF_RULE),
        ("LINEABOVE",     (0, 2), (-1, 2), 0.5, BF_RULE),
        ("LINEAFTER",     (0, 0), (0, -1), 0.5, BF_RULE),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
    ]))
    return t


# ── PDF builders ─────────────────────────────────────────────────────────────

def build_free_pdf(*,
    beat_title: str, producer: str,
    licensee_name: str, licensee_email: str,
    reference: str, date_str: str,
) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=32 * mm, bottomMargin=20 * mm,
        title=f"BeatFinder Free Licence — {beat_title}",
        author="BeatFinder",
    )
    S = _styles()
    story = []

    story.append(Paragraph("FREE NON-EXCLUSIVE BEAT LICENCE", S["title"]))
    story.append(Paragraph(f"Reference: {reference} &nbsp;|&nbsp; Date: {date_str}", S["subtitle"]))

    story.append(_info_table([
        ["Beat",      f'"{beat_title}"'],
        ["Producer",  producer],
        ["Licensee",  f"{licensee_name} ({licensee_email})"],
        ["Type",      "FREE Non-Exclusive — Non-Commercial"],
        ["Fee",       "£0.00"],
    ]))
    story.append(Spacer(1, 14))

    sections = [
        ("1. GRANT OF LICENCE",
         "The Producer hereby grants the Licensee a free, non-exclusive, "
         "non-transferable, worldwide licence to use the Beat solely for "
         "non-commercial purposes, on the terms set out in this Agreement."),
        ("2. PERMITTED USES",
         "Recording one (1) song featuring the Beat; sharing the resulting "
         "recording on free streaming platforms (e.g. SoundCloud and YouTube "
         "non-monetised); promotion on personal social media; and performing "
         "the recording at non-ticketed live events. The Licensee must include "
         f'the credit "(Prod. by {producer})" in the description, metadata or '
         "liner notes of every distributed or published copy of the recording."),
        ("3. RESTRICTIONS",
         "The Licensee shall NOT: (a) monetise the resulting recording on any "
         "platform; (b) sell, lease, sub-licence or otherwise transfer the "
         "Beat or any derivative work; (c) claim authorship of or copyright "
         "in the Beat; (d) register the Beat or any derivative work with a "
         "content-identification or rights-collection service "
         "(e.g. ContentID, Audiam, BMI, ASCAP, PRS) under their own name. "
         "If the resulting recording exceeds 10,000 cumulative streams across "
         "all platforms, or begins to generate revenue by any means, the "
         "Licensee must purchase a paid lease before further distribution."),
        ("4. OWNERSHIP AND COPYRIGHT",
         "The Producer retains all rights, title and interest in and to the "
         "Beat, including all copyrights and master ownership. The Licensee "
         "owns only their original vocal performance and lyrics, where applicable."),
        ("5. WARRANTIES",
         "The Producer warrants that the Beat is their original work. The "
         "Licensee warrants that any vocals or lyrics they add are their "
         "original work and do not infringe any third-party rights."),
        ("6. PLATFORM",
         "BeatFinder (beatfinder.co.uk) facilitates this Licence as a "
         "platform provider only and is not a party to this Agreement. "
         "BeatFinder accepts no liability for any dispute arising between "
         "the Producer and Licensee."),
        ("7. TERMINATION",
         "This Licence terminates automatically if the Licensee breaches any "
         "of its terms. Upon termination, the Licensee must cease all use "
         "and distribution of the Beat and any derivative works."),
        ("8. GOVERNING LAW",
         "This Agreement shall be governed by and construed in accordance "
         "with the laws of England and Wales, and the parties submit to the "
         "exclusive jurisdiction of the English courts."),
    ]
    for heading, body in sections:
        story.append(Paragraph(heading, S["h2"]))
        story.append(Paragraph(body, S["body"]))

    story.append(Spacer(1, 16))
    story.append(_signature_block(licensee_name, licensee_email, reference, date_str))
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "By downloading the Beat through BeatFinder, the Licensee confirms "
        "they have read, understood and accepted the terms of this Licence.",
        S["small"]
    ))

    def _page(c, d): _draw_chrome(c, d, kind="FREE", ref=reference)
    doc.build(story, onFirstPage=_page, onLaterPages=_page)
    return buf.getvalue()


def build_lease_pdf(*,
    beat_title: str, producer: str,
    licensee_name: str, licensee_email: str,
    price: str, reference: str, date_str: str,
) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=32 * mm, bottomMargin=20 * mm,
        title=f"BeatFinder Lease — {beat_title}",
        author="BeatFinder",
    )
    S = _styles()
    story = []

    story.append(Paragraph("NON-EXCLUSIVE BEAT LEASE AGREEMENT", S["title"]))
    story.append(Paragraph(f"Reference: {reference} &nbsp;|&nbsp; Date: {date_str}", S["subtitle"]))

    story.append(_info_table([
        ["Beat",      f'"{beat_title}"'],
        ["Producer",  producer],
        ["Licensee",  f"{licensee_name} ({licensee_email})"],
        ["Licence",   "MP3 Non-Exclusive Lease"],
        ["Fee",       price or "£0.00"],
        ["Payment",   "Confirmed via Stripe"],
    ]))
    story.append(Spacer(1, 14))

    sections = [
        ("1. GRANT OF LICENCE",
         "Subject to receipt of the Fee, the Producer grants the Licensee a "
         "non-exclusive, non-transferable, worldwide licence to use the Beat "
         "in the creation, recording, performance and commercial distribution "
         "of one (1) new musical composition (the \"New Work\")."),
        ("2. PERMITTED USES",
         "The Licensee may (a) commercially distribute the New Work on all "
         "monetised streaming and download platforms (including Spotify, "
         "Apple Music, YouTube monetised, TikTok and similar); (b) perform "
         "the New Work at paid and unpaid live events; (c) make up to "
         "100,000 cumulative paid downloads and/or streams across all "
         f'platforms; (d) use the New Work in non-broadcast video content. '
         f'The Licensee must include the credit "(Prod. by {producer})" in '
         "the metadata, description or liner notes of every release."),
        ("3. RESTRICTIONS",
         "The Licensee shall NOT: (a) sell, lease, sub-licence or otherwise "
         "transfer the Beat itself, in whole or in part, to any third party; "
         "(b) claim authorship of or copyright in the Beat; (c) register the "
         "Beat as their own composition with any rights organisation; "
         "(d) use the Beat in any broadcast advertisement, film or "
         "television sync without a separate written agreement; (e) exceed "
         "the 100,000 stream/download threshold without upgrading to an "
         "exclusive licence."),
        ("4. OWNERSHIP",
         "The Producer retains full copyright and master ownership of the "
         "Beat. The Licensee owns the New Work as a derivative composition. "
         "Composition royalties shall be split 50% to the Producer and 50% "
         "to the Licensee. Master recording royalties belong 100% to the Licensee."),
        ("5. CREDIT REQUIREMENT",
         f'All public uses of the New Work must include the credit '
         f'"Produced by {producer}" or "(Prod. by {producer})" in a visible '
         "location appropriate to the medium (track metadata, video "
         "description, liner notes or on-screen credits)."),
        ("6. WARRANTIES",
         "The Producer warrants that the Beat is their original work, free "
         "from third-party claims, and that they have full authority to "
         "grant this Licence. The Licensee warrants that any vocals, lyrics "
         "or additional production they add are their original work."),
        ("7. PLATFORM",
         "BeatFinder (beatfinder.co.uk) facilitates this Agreement and the "
         "associated payment as a platform provider only and is not a party "
         "to this Agreement. BeatFinder accepts no liability for any dispute "
         "between the Producer and Licensee."),
        ("8. TERM AND TERMINATION",
         "This Licence is perpetual unless terminated for breach. Upon "
         "termination for breach, the Licensee must cease all distribution "
         "of the New Work; any prior good-faith distribution remains licensed."),
        ("9. GOVERNING LAW",
         "This Agreement shall be governed by and construed in accordance "
         "with the laws of England and Wales, and the parties submit to the "
         "exclusive jurisdiction of the English courts."),
    ]
    for heading, body in sections:
        story.append(Paragraph(heading, S["h2"]))
        story.append(Paragraph(body, S["body"]))

    story.append(Spacer(1, 16))
    story.append(_signature_block(licensee_name, licensee_email, reference, date_str))
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "By completing payment through BeatFinder, the Licensee confirms "
        "they have read, understood and accepted the terms of this Lease.",
        S["small"]
    ))

    def _page(c, d): _draw_chrome(c, d, kind="LEASE", ref=reference)
    doc.build(story, onFirstPage=_page, onLaterPages=_page)
    return buf.getvalue()


# ── Routes ───────────────────────────────────────────────────────────────────

class FreeContractBody(BaseModel):
    beat_id:    Optional[str] = None
    beat_title: str
    producer:   Optional[str] = "Producer"


def _pdf_response(pdf_bytes: bytes, filename: str) -> Response:
    safe = _safe_filename(filename)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition":         f'attachment; filename="{safe}.pdf"',
            "Content-Type":                "application/pdf",
            "X-Content-Type-Options":      "nosniff",
            "Cache-Control":               "no-cache, no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.options("/free")
async def free_options():
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        },
    )


@router.post("/free")
async def free_contract_pdf(
    body: FreeContractBody,
    request: Request,
    user=Depends(get_current_user),
):
    """Generate a polished PDF for a free-beat licence acceptance."""
    db = request.app.state.db

    # If a beat_id is supplied, look up the canonical producer/title from the DB
    producer = body.producer or "Producer"
    beat_title = body.beat_title or "Beat"
    if body.beat_id:
        try:
            from bson import ObjectId
            beat = await db.producer_beats.find_one({"_id": ObjectId(body.beat_id)})
            if beat:
                producer   = beat.get("producer") or producer
                beat_title = beat.get("title")    or beat_title
        except Exception:
            pass

    licensee_name  = user.get("name") or user.get("username") or "Licensee"
    licensee_email = user.get("email") or ""
    reference      = "BF-FREE-" + str(int(datetime.utcnow().timestamp() * 1000))
    date_str       = _gbp_date(datetime.utcnow())

    # Persist a record of the acceptance for audit/legal trail
    try:
        await db.contract_acceptances.insert_one({
            "type":           "free",
            "reference":      reference,
            "beat_id":        body.beat_id,
            "beat_title":     beat_title,
            "producer":       producer,
            "licensee_id":    str(user["_id"]),
            "licensee_name":  licensee_name,
            "licensee_email": licensee_email,
            "accepted_at":    datetime.utcnow(),
            "ip":             (request.client.host if request.client else None),
            "user_agent":     request.headers.get("user-agent", ""),
        })
    except Exception:
        pass  # audit log shouldn't block the download

    pdf = build_free_pdf(
        beat_title=beat_title, producer=producer,
        licensee_name=licensee_name, licensee_email=licensee_email,
        reference=reference, date_str=date_str,
    )
    return _pdf_response(pdf, f"BeatFinder_FreeLicence_{beat_title}")


@router.options("/lease/{lease_id}")
async def lease_options(lease_id: str):
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        },
    )


@router.get("/lease/{lease_id}")
async def lease_contract_pdf(
    lease_id: str,
    request: Request,
    user=Depends(get_current_user),
):
    """Generate a polished PDF for a purchased lease. Owner-only access."""
    db = request.app.state.db
    from bson import ObjectId

    lease = None
    try:
        lease = await db.purchased_leases.find_one({"_id": ObjectId(lease_id)})
    except Exception:
        # The id might be stored as a plain string in some legacy records
        lease = await db.purchased_leases.find_one({"_id": lease_id})

    if not lease:
        raise HTTPException(status_code=404, detail="Lease not found")

    # Only the buyer (or an admin) may download
    if lease.get("buyer_id") != str(user["_id"]) and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Not your lease")

    beat_title = lease.get("beat_title") or "Beat"
    producer   = lease.get("producer")   or "Producer"

    # Fallback to fetching producer from the beat doc if missing
    if (not producer or producer == "Producer") and lease.get("beat_id"):
        try:
            beat = await db.producer_beats.find_one({"_id": ObjectId(lease["beat_id"])})
            if beat:
                producer = beat.get("producer") or producer
        except Exception:
            pass

    licensee_name  = lease.get("buyer_name")  or user.get("name") or user.get("username") or "Licensee"
    licensee_email = lease.get("buyer_email") or user.get("email") or ""
    price          = lease.get("price") or ""
    reference      = "BF-LEASE-" + str(lease["_id"])
    date_str       = _gbp_date(lease.get("purchased_at") or datetime.utcnow())

    pdf = build_lease_pdf(
        beat_title=beat_title, producer=producer,
        licensee_name=licensee_name, licensee_email=licensee_email,
        price=price, reference=reference, date_str=date_str,
    )
    return _pdf_response(pdf, f"BeatFinder_Lease_{beat_title}_{lease_id}")
