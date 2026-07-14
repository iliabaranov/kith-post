"""Send an event's queued invitations.

Per recipient: build the MIME, then by mode —
  dry-run   -> write data/outbox/<event>/<recipient>.eml (no Gmail call)
  self-only -> send via Gmail, but To = the logged-in user (test against your inbox)
  live      -> send via Gmail to the recipient
Status flips queued -> sent (committed per recipient, so a crash never double-sends);
failures are logged and left 'queued' so they can be retried.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kith.config import SendMode, Settings
from kith.core import mailbuild
from kith.db.models import Asset, Event, Recipient, User
from kith.services.gmail import GmailAuthError

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


def send_event(
    db: Session, event: Event, user: User, settings: Settings, *, note: str | None = None
) -> SendResult:
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

    # Cc riders: only for cards without RSVP, and never during a self-only test
    # (that would email the family while you're just testing your own inbox).
    cc_list = None
    if event.cc and not rsvp and settings.send_mode != SendMode.self_only:
        try:
            cc_list = [(c.get("name") or None, c["email"]) for c in json.loads(event.cc)]
        except (ValueError, KeyError, TypeError):
            cc_list = None

    for r in recipients:
        view_url = f"{base}/i/{r.token}"
        common = dict(
            title=event.title, message=event.message, host_name=host_name,
            view_url=view_url,
        )
        html = mailbuild.invite_html(has_image=bool(image_bytes), rsvp=rsvp, note=note, **common)
        text = mailbuild.invite_text(rsvp=rsvp, note=note, **common)
        to_email = user.email if settings.send_mode == SendMode.self_only else r.email
        # Threading: the first send stamps an anchor Message-ID we keep; a re-send
        # (recipient already has one) references that anchor so Gmail threads it.
        anchor = r.rfc822_message_id
        # Use the sender's own domain so Gmail is more likely to preserve the
        # Message-ID (it overwrites IDs whose domain doesn't match the account),
        # which is what lets reminders/re-sends thread via References.
        mid_domain = user.email.rsplit("@", 1)[-1] or "kith.post"
        this_id = mailbuild.make_message_id(mid_domain)
        msg = mailbuild.build_email(
            subject=mailbuild.subject_for(event.title, rsvp),
            from_name=user.display_name, from_email=user.email,
            to_email=to_email, to_name=r.name, html=html, text=text,
            image_bytes=image_bytes, image_subtype=image_subtype,
            message_id=this_id, in_reply_to=anchor, references=anchor,
            cc=cc_list,
        )
        try:
            if settings.send_mode == SendMode.dry_run:
                _write_outbox(settings, event.id, r.id, msg)
            else:
                from kith.services import gmail

                res = gmail.gmail_send(
                    settings, user.refresh_token, mailbuild.to_raw(msg), thread_id=r.thread_id
                )
                r.msg_id_hdr = res.get("id")
                r.thread_id = res.get("threadId")
            if not anchor:  # remember the first message as the thread anchor
                r.rfc822_message_id = this_id
            r.status = "sent"
            r.sent_at = datetime.now(UTC)
            if user.reconnect_needed:
                user.reconnect_needed = False  # the token works again
            sent += 1
            db.commit()
        except GmailAuthError:
            user.reconnect_needed = True  # expired/revoked token — prompt a reconnect
            log.warning("Google connection invalid for user %s — reconnect needed", user.id)
            failed += 1
            db.commit()
            break  # a dead token fails every recipient; stop early
        except Exception:
            log.exception("send failed for recipient %s (event %s)", r.id, event.id)
            failed += 1  # leave as 'queued' so a retry can pick it up
            db.commit()

    return SendResult(sent=sent, failed=failed, mode=settings.send_mode.value)
