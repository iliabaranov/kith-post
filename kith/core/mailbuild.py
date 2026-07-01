"""Build the per-recipient invitation email — pure, testable.

multipart/alternative (plain + HTML) with the card image inlined via a CID so it
renders offline. NO tracking pixel — the only signal is the recipient clicking
through to their invitation page. Email HTML must use inline styles.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import formataddr
from html import escape

IMAGE_CID = "cardimage"


def subject_for(title: str, rsvp: bool) -> str:
    title = (title or "").strip() or "A card for you"
    return f"You're invited: {title}" if rsvp else title


def invite_html(
    *, title: str, message: str, host_name: str,
    view_url: str, has_image: bool, rsvp: bool,
) -> str:
    cta = "View invitation &amp; RSVP" if rsvp else "View your card"
    img = (
        f'<img src="cid:{IMAGE_CID}" alt="" width="520" '
        'style="display:block;width:100%;max-width:520px;height:auto;border-radius:12px;'
        'border:1px solid #E4D8C4;margin:0 0 20px;">'
        if has_image else ""
    )
    msg_html = (
        f'<p style="margin:0 0 20px;font-size:16px;line-height:1.6;color:#3B2A33;">'
        f"{escape(message).replace(chr(10), '<br>')}</p>"
        if message else ""
    )
    sans = "font-family:Helvetica,Arial,sans-serif;"
    title_html = escape(title or "You're invited")
    return f"""\
<!doctype html><html><body style="margin:0;background:#F4ECDD;">
<div style="max-width:560px;margin:0 auto;padding:28px 20px;
  font-family:Georgia,'Times New Roman',serif;">
  {img}
  <h1 style="margin:0 0 12px;font-size:28px;line-height:1.15;color:#3B2A33;">{title_html}</h1>
  {msg_html}
  <p style="margin:0 0 24px;font-size:15px;color:#6E5C63;{sans}">
    {escape(host_name)} sent you {"an invitation" if rsvp else "a card"} with Kith Post.</p>
  <a href="{escape(view_url)}" style="display:inline-block;background:#E2972B;color:#3B2A33;
    text-decoration:none;font-weight:bold;{sans}
    padding:14px 24px;border-radius:12px;">{cta}</a>
  <p style="margin:28px 0 0;font-size:12px;color:#6E5C63;{sans}">
    Sent with Kith Post ·
    <a href="{escape(view_url)}" style="color:#6B3A57;">{escape(view_url)}</a></p>
</div></body></html>"""


def invite_text(
    *, title: str, message: str, host_name: str,
    view_url: str, rsvp: bool,
) -> str:
    lines = [title or "You're invited"]
    if message:
        lines += ["", message]
    lines += [
        "",
        f"{host_name} sent you {'an invitation' if rsvp else 'a card'} with Kith Post.",
        "",
        f"{'View it and RSVP' if rsvp else 'View your card'}: {view_url}",
    ]
    return "\n".join(lines) + "\n"


def build_email(
    *, subject: str, from_name: str, from_email: str, to_email: str,
    to_name: str | None = None, html: str, text: str,
    image_bytes: bytes | None = None, image_subtype: str = "jpeg",
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
    msg["To"] = formataddr((to_name, to_email)) if to_name else to_email
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    if image_bytes:
        html_part = msg.get_payload()[1]
        html_part.add_related(
            image_bytes, maintype="image", subtype=image_subtype, cid=f"<{IMAGE_CID}>"
        )
    return msg


def to_raw(msg: EmailMessage) -> str:
    """base64url-encoded RFC822, as the Gmail API's messages.send wants."""
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()
