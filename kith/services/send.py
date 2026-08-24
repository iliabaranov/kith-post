"""Send an event's queued invitations, over whichever channel each is on.

Email, per recipient: build the MIME, then by mode —
  dry-run   -> write data/outbox/<event>/<recipient>.eml (no Gmail call)
  self-only -> send via Gmail, but To = the logged-in user (test against your inbox)
  live      -> send via Gmail to the recipient

WhatsApp, per recipient: compose the text, then by mode —
  dry-run   -> write data/outbox/<event>/whatsapp/<recipient>.txt (no WAHA call)
  self-only -> send to the host's own linked number
  live      -> send to the recipient's number

Status flips queued -> sent (committed per recipient, so a crash never double-sends);
failures are logged and left 'queued' so they can be retried.

The two channels differ in one important way: a WhatsApp batch can be stopped by
WhatsApp itself. The session is pre-flighted once, and a reachout timelock or an
exhausted new-chat quota aborts the rest of the batch rather than burning attempt
after attempt — every further attempt while restricted makes the account look
worse, and the recipients stay 'queued' for when it lifts.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kith.config import SendMode, Settings
from kith.core import mailbuild, wamessage
from kith.db.models import Asset, Event, Recipient, User
from kith.services import wa_session as wa_link
from kith.services import waha
from kith.services.gmail import GmailAuthError

log = logging.getLogger("kith")

_SUBTYPE = {"image/jpeg": "jpeg", "image/png": "png", "image/webp": "webp"}


@dataclass
class SendResult:
    """Totals across both channels, plus what WhatsApp did (or refused to do)."""

    sent: int
    failed: int
    mode: str
    wa_sent: int = 0
    wa_failed: int = 0
    # Set when the WhatsApp batch was stopped as a whole (not linked, timelocked,
    # capped, WAHA unreachable). Distinct from wa_failed, which counts individual
    # recipients — this is "we stopped on purpose", and it's what the UI explains.
    wa_blocked: str | None = None


def _write_outbox(settings: Settings, event_id: str, recipient_id: str, msg) -> None:
    d = settings.outbox_dir / event_id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{recipient_id}.eml").write_bytes(msg.as_bytes())


def _write_wa_outbox(
    settings: Settings, event_id: str, recipient_id: str, to: str, text: str
) -> None:
    """dry-run's WhatsApp equivalent of an .eml — the exact text, and who to."""
    d = settings.outbox_dir / event_id / "whatsapp"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{recipient_id}.txt").write_text(f"To: {to}\n\n{text}\n")


def send_event(
    db: Session, event: Event, user: User, settings: Settings, *, note: str | None = None
) -> SendResult:
    queued = db.execute(
        select(Recipient).where(Recipient.event_id == event.id, Recipient.status == "queued")
    ).scalars().all()
    # A recipient is on exactly one channel; phone set == WhatsApp (channel is
    # NULL on rows that predate it, so the number is the reliable signal).
    recipients = [r for r in queued if not r.phone]
    wa_recipients = [r for r in queued if r.phone]
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

    wa = (
        _send_whatsapp(db, event, user, settings, wa_recipients, note=note)
        if wa_recipients
        else _WaOutcome(0, 0, None)
    )
    return SendResult(
        sent=sent + wa.sent,
        failed=failed + wa.failed,
        mode=settings.send_mode.value,
        wa_sent=wa.sent,
        wa_failed=wa.failed,
        wa_blocked=wa.blocked,
    )


@dataclass
class _WaOutcome:
    sent: int
    failed: int
    blocked: str | None


def _send_whatsapp(
    db: Session,
    event: Event,
    user: User,
    settings: Settings,
    recipients: list[Recipient],
    *,
    note: str | None = None,
) -> _WaOutcome:
    """Send this event's WhatsApp invitations. Never raises — it reports."""
    rsvp = bool((event.blocks or {}).get("rsvp"))
    base = settings.base_url.rstrip("/")
    host_name = user.display_name or "A friend"
    when = wamessage.when_line(event.event_date, event.event_time)
    dry = settings.send_mode == SendMode.dry_run

    client = None
    if not dry:
        # One pre-flight for the whole batch: re-reads the live session, and
        # refuses on not-linked / timelocked / capped before anything is sent.
        try:
            wa_link.sendable(db, user, settings)
        except waha.Timelocked as e:
            user.wa_timelock_until = e.ends_at
            db.commit()
            log.warning("whatsapp: reachout timelock active for user %s", user.id)
            return _WaOutcome(0, 0, "timelock")
        except waha.Capped:
            log.warning("whatsapp: new-chat quota exhausted for user %s", user.id)
            return _WaOutcome(0, 0, "capped")
        except waha.NotLinked:
            return _WaOutcome(0, 0, "not-linked")
        except waha.WahaError:
            log.exception("whatsapp: WAHA unreachable for user %s", user.id)
            return _WaOutcome(0, 0, "unavailable")
        client = wa_link.client(settings)

    sent = failed = 0
    for i, r in enumerate(recipients):
        text = wamessage.invite_text(
            title=event.title,
            host_name=host_name,
            view_url=f"{base}/i/{r.token}",
            recipient_name=r.name,
            when=when,
            rsvp=rsvp,
            note=note,
        )
        to = user.wa_number if settings.send_mode == SendMode.self_only else r.phone
        try:
            if dry:
                _write_wa_outbox(settings, event.id, r.id, to or r.phone, text)
            else:
                # Ask WhatsApp whether the number is really there first. Messaging
                # numbers that aren't on WhatsApp is one of the things that earns
                # an account a reachout timelock, so a wrong digit should cost one
                # recipient, not the host's ability to send at all. A check that
                # errors is not treated as an answer — we go ahead and send.
                chat_id = None
                try:
                    check = client.check_exists(user.wa_session, to)
                    if not check.exists:
                        log.warning("whatsapp: %s is not on WhatsApp (event %s)", r.id, event.id)
                        failed += 1
                        continue
                    chat_id = check.chat_id
                except waha.WahaError:
                    log.warning("whatsapp: existence check failed for %s; sending anyway", r.id)
                res = client.send_text(user.wa_session, to, text, chat_id=chat_id)
                r.wa_message_id = res.get("id")
            r.status = "sent"
            r.sent_at = datetime.now(UTC)
            sent += 1
            db.commit()
        except waha.Timelocked as e:
            # Hit mid-batch: stop. Retrying is what makes it worse, and the rest
            # stay queued so they go out once it lifts.
            user.wa_timelock_until = e.ends_at
            db.commit()
            log.warning("whatsapp: timelock hit mid-send for user %s; stopping", user.id)
            return _WaOutcome(sent, failed, "timelock")
        except waha.Capped:
            db.commit()
            return _WaOutcome(sent, failed, "capped")
        except waha.NotLinked:
            db.commit()
            return _WaOutcome(sent, failed, "not-linked")
        except Exception:
            log.exception("whatsapp send failed for recipient %s (event %s)", r.id, event.id)
            failed += 1  # left 'queued' so a retry can pick it up
            db.commit()
        # Space the sends out; a burst to people who've never had a message from
        # this number is exactly the pattern WhatsApp restricts.
        if not dry and i + 1 < len(recipients) and settings.waha_send_gap_seconds > 0:
            time.sleep(settings.waha_send_gap_seconds)

    return _WaOutcome(sent, failed, None)
