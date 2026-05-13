"""
email.py — Send emails via Resend.

Centralises the Resend HTTP call so password resets, contact forms,
and any future transactional emails go through one place.

Env vars:
  RESEND_API_KEY — get from https://resend.com/api-keys
  FROM_EMAIL     — must be on a verified domain in Resend
                   defaults to support@beatfinder.co.uk
"""
import os
import httpx
from typing import Optional

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL     = os.getenv("FROM_EMAIL", "BeatFinder <support@beatfinder.co.uk>")
SUPPORT_EMAIL  = os.getenv("SUPPORT_EMAIL", "support@beatfinder.co.uk")

RESEND_URL = "https://api.resend.com/emails"


async def send_email(
    to: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> bool:
    """
    Send an email through Resend.

    Returns True on success, False on failure (the caller decides whether to
    raise — for password resets we DO NOT want to leak failure details to the
    requester, so we always return success to the API caller even if email
    sending fails internally).
    """
    if not RESEND_API_KEY:
        print("[email] RESEND_API_KEY not configured — email NOT sent")
        return False

    payload = {
        "from":    FROM_EMAIL,
        "to":      [to],
        "subject": subject,
        "html":    html,
    }
    if text:
        payload["text"] = text
    if reply_to:
        payload["reply_to"] = reply_to

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                RESEND_URL,
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json=payload,
            )
            if r.status_code >= 400:
                print(f"[email] Resend {r.status_code}: {r.text}")
                return False
            return True
    except Exception as e:
        print(f"[email] Send error: {e}")
        return False


# ── Email templates ──────────────────────────────────────────────────────────

def password_reset_template(reset_link: str, user_name: str = "there") -> tuple[str, str]:
    """Returns (html, plain_text) for a password reset email."""
    html = f"""\
<!DOCTYPE html>
<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a0a;color:#fff;margin:0;padding:0;">
  <div style="max-width:560px;margin:0 auto;padding:40px 20px;">
    <h1 style="font-family:'Bebas Neue',sans-serif;font-size:36px;letter-spacing:2px;color:#fff;margin:0 0 8px;">BEATFINDER</h1>
    <div style="height:3px;width:60px;background:#C026D3;margin-bottom:30px;"></div>

    <h2 style="color:#fff;font-size:22px;font-weight:800;margin:0 0 16px;">Reset your password</h2>
    <p style="color:#ccc;font-size:15px;line-height:1.6;margin:0 0 24px;">
      Hi {user_name}, we received a request to reset the password for your BeatFinder account.
      Tap the button below to set a new one. This link expires in 1 hour.
    </p>

    <a href="{reset_link}" style="display:inline-block;background:#C026D3;color:#fff;text-decoration:none;font-weight:800;padding:14px 28px;border-radius:32px;font-size:15px;margin:8px 0 28px;">
      Reset Password
    </a>

    <p style="color:#888;font-size:13px;line-height:1.6;margin:0 0 8px;">
      If the button doesn't work, copy and paste this link into your browser:
    </p>
    <p style="color:#06B6D4;font-size:12px;word-break:break-all;margin:0 0 32px;">
      {reset_link}
    </p>

    <hr style="border:none;border-top:1px solid #222;margin:32px 0;">
    <p style="color:#666;font-size:12px;line-height:1.6;margin:0;">
      Didn't request this? You can ignore this email — your password won't change.
      If you have concerns, reach us at <a href="mailto:support@beatfinder.co.uk" style="color:#06B6D4;text-decoration:none;">support@beatfinder.co.uk</a>.
    </p>
  </div>
</body>
</html>"""
    text = (
        f"Hi {user_name},\n\n"
        f"We received a request to reset the password for your BeatFinder account.\n\n"
        f"Click this link to set a new password (expires in 1 hour):\n{reset_link}\n\n"
        f"If you didn't request this, you can ignore this email — your password won't change.\n\n"
        f"— BeatFinder Support\nsupport@beatfinder.co.uk"
    )
    return html, text


def contact_form_template(name: str, email: str, subject: str, message: str) -> tuple[str, str]:
    """Returns (html, plain_text) for a contact form submission email to support."""
    # Escape user input to prevent HTML injection
    import html as html_escape
    safe_name    = html_escape.escape(name)
    safe_email   = html_escape.escape(email)
    safe_subject = html_escape.escape(subject)
    safe_message = html_escape.escape(message).replace("\n", "<br>")

    html_body = f"""\
<!DOCTYPE html>
<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;color:#1a1a1a;margin:0;padding:0;">
  <div style="max-width:600px;margin:0 auto;padding:30px 20px;">
    <div style="background:#fff;border-radius:12px;padding:30px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
      <div style="border-bottom:2px solid #C026D3;padding-bottom:14px;margin-bottom:20px;">
        <strong style="font-size:18px;color:#C026D3;letter-spacing:1px;">SUPPORT REQUEST</strong>
      </div>

      <p style="margin:0 0 6px;color:#666;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;">From</p>
      <p style="margin:0 0 16px;color:#1a1a1a;font-size:15px;">
        {safe_name} &lt;<a href="mailto:{safe_email}" style="color:#C026D3;text-decoration:none;">{safe_email}</a>&gt;
      </p>

      <p style="margin:0 0 6px;color:#666;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;">Subject</p>
      <p style="margin:0 0 16px;color:#1a1a1a;font-size:15px;font-weight:600;">{safe_subject}</p>

      <p style="margin:0 0 6px;color:#666;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;">Message</p>
      <div style="background:#f9f9f9;border-left:3px solid #C026D3;padding:14px 16px;border-radius:4px;color:#333;font-size:14px;line-height:1.6;">
        {safe_message}
      </div>

      <p style="margin:24px 0 0;padding-top:16px;border-top:1px solid #eee;color:#999;font-size:12px;">
        Reply to this email to respond directly to the user.
      </p>
    </div>
  </div>
</body>
</html>"""
    text_body = (
        f"SUPPORT REQUEST\n\n"
        f"From: {name} <{email}>\n"
        f"Subject: {subject}\n\n"
        f"Message:\n{message}\n\n"
        f"---\nReply to this email to respond directly to the user."
    )
    return html_body, text_body
