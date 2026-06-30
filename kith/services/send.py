"""Send an event's queued invitations.

Per recipient: build the MIME, then by mode —
  dry-run   -> write data/outbox/<event>/<recipient>.eml (no Gmail call)
  self-only -> send via Gmail, but To = the logged-in user (test against your inbox)
  live      -> send via Gmail to the recipient
Status flips queued -> sent (committed per recipient, so a crash never double-sends);
failures are logged and left 'queued' so they can be retried.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kith.config import SendMode, Settings
from kith.core import mailbuild
from kith.db.models import Asset, Event, Recipient, User

log = logging.getLogger("kith")

_SUBTYPE = {"image/jpeg": "jpeg", "image/png": "png", "image/webp": "webp"}


@dataclass
class SendResult:
    sent: int
    failed: int
    mode: str


def _write_outbox(settings: Settings, event_id: str, recipient_id: str, msg) -> None:
    d = settings.outbox_dir / event_id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{recipient_id}.eml").write_bytes(msg.as_bytes())


def send_event(db: Session, event: Event, user: User, settings: Settings) -> SendResult:
    recipients = db.execute(
        select(Recipient).where(Recipient.event_id == event.id, Recipient.status == "queued")
    ).scalars().all()
    asset = db.get(Asset, event.asset_id) if event.asset_id else None
    image_bytes = Path(asset.inline_path).read_bytes() if asset else None
    image_subtype = _SUBTYPE.get(asset.mime, "jpeg") if asset else "jpeg"

    rsvp = bool((event.blocks or {}).get("rsvp"))
    base = settings.base_url.rstrip("/")
    host_name = user.display_name or "A friend"
    sent = failed = 0

    for r in recipients:
        view_url = f"{base}/i/{r.token}"
        common = dict(
            title=event.title, message=event.message, host_name=host_name,
            recipient_name=r.name, view_url=view_url,
        )
        html = mailbuild.invite_html(has_image=bool(image_bytes), rsvp=rsvp, **common)
        text = mailbuild.invite_text(rsvp=rsvp, **common)
        to_email = user.email if settings.send_mode == SendMode.self_only else r.email
        msg = mailbuild.build_email(
            subject=mailbuild.subject_for(event.title, rsvp),
            from_name=user.display_name, from_email=user.email,
            to_email=to_email, to_name=r.name, html=html, text=text,
            image_bytes=image_bytes, image_subtype=image_subtype,
        )
        try:
            if settings.send_mode == SendMode.dry_run:
                _write_outbox(settings, event.id, r.id, msg)
            else:
                from kith.services import gmail

                res = gmail.gmail_send(settings, user.refresh_token, mailbuild.to_raw(msg))
                r.msg_id_hdr = res.get("id")
                r.thread_id = res.get("threadId")
            r.status = "sent"
            r.sent_at = datetime.now(UTC)
            sent += 1
        except Exception:
            log.exception("send failed for recipient %s (event %s)", r.id, event.id)
            failed += 1  # leave as 'queued' so a retry can pick it up
        db.commit()

    return SendResult(sent=sent, failed=failed, mode=settings.send_mode.value)
