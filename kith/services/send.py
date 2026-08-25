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

import contextlib
import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kith.config import SendMode, Settings
from kith.core import eventkind, mailbuild, wamessage
from kith.db.models import Asset, Event, Recipient, User
from kith.services import wa_session as wa_link
from kith.services import waha
from kith.services.gmail import GmailAuthError

log = logging.getLogger("kith")

_SUBTYPE = {"image/jpeg": "jpeg", "image/png": "png", "image/webp": "webp"}


def _far_future() -> datetime:
    """A stand-in end date for a timelock WhatsApp didn't date.

    ``wa_timelock_until`` is the only place the UI learns a send is blocked, so
    storing None for "restricted, end unknown" is indistinguishable from "not
    restricted" — the host would get a stalled queue and no explanation. A day
    out is honest enough: the next successful read of the session replaces it.
    """
    return datetime.now(UTC) + timedelta(days=1)


def _pace(settings: Settings, dry: bool, index: int, total: int) -> None:
    """Wait before the next send. A burst is what WhatsApp restricts accounts
    for, and an exactly-even cadence is its own signature — neither looks like a
    person working through a list.

    Called from the skip paths too: a list with several bad numbers would
    otherwise fire its existence checks back to back, which is the same burst.
    """
    if dry or index + 1 >= total:
        return
    gap = next_send_gap(settings)
    if gap > 0:
        log.info("whatsapp: pausing %.1fs before the next send", gap)
        time.sleep(gap)


def next_send_gap(settings: Settings, rng: random.Random | None = None) -> float:
    """A fresh random pause, in seconds, to put between two WhatsApp sends.

    Uniform over the configured range. The randomness is the point: a fixed gap
    is as machine-like as no gap at all, only slower.
    """
    lo = max(0.0, settings.waha_send_gap_min_seconds)
    hi = max(lo, settings.waha_send_gap_max_seconds)
    if hi <= 0:
        return 0.0
    return (rng or random).uniform(lo, hi)


@dataclass
class SendResult:
    """Totals across both channels, plus what WhatsApp did (or refused to do)."""

    sent: int
    failed: int
    mode: str
    wa_sent: int = 0
    wa_failed: int = 0
    # Handed off to a background batch rather than sent during the request. With
    # a 5-20s pause between messages a family-sized list takes minutes, which is
    # far longer than an HTTP request should live.
    wa_pending: int = 0
    # Set when the WhatsApp batch was stopped as a whole (not linked, timelocked,
    # capped, WAHA unreachable). Distinct from wa_failed, which counts individual
    # recipients — this is "we stopped on purpose", and it's what the UI explains.
    wa_blocked: str | None = None


def _write_outbox(settings: Settings, event_id: str, recipient_id: str, msg) -> None:
    d = settings.outbox_dir / event_id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{recipient_id}.eml").write_bytes(msg.as_bytes())


def _write_wa_outbox(
    settings: Settings, event_id: str, recipient_id: str, to: str, text: str,
    card_bytes: int | None = None,
) -> None:
    """dry-run's WhatsApp equivalent of an .eml — the exact text, and who to."""
    d = settings.outbox_dir / event_id / "whatsapp"
    d.mkdir(parents=True, exist_ok=True)
    card = f"Card: {card_bytes} bytes, sent as the image with this as its caption\n" \
        if card_bytes else "Card: none — text only\n"
    (d / f"{recipient_id}.txt").write_text(f"To: {to}\n{card}\n{text}\n")


def send_event(
    db: Session, event: Event, user: User, settings: Settings, *,
    note: str | None = None, wa_defer: bool = False,
) -> SendResult:
    queued = db.execute(
        select(Recipient).where(Recipient.event_id == event.id, Recipient.status == "queued")
    ).scalars().all()
    # A recipient is on exactly one channel; phone set == WhatsApp (channel is
    # NULL on rows that predate it, so the number is the reliable signal).
    recipients = [r for r in queued if not r.phone]
    wa_recipients = [r for r in queued if r.phone]
    asset = db.get(Asset, event.asset_id) if event.asset_id else None
    # A missing inline file must not take the whole send down with it. Assets can
    # outlive their files — the retention sweep removes the full-res copy, and
    # older rows exist whose inline copy is gone too. A card without its picture
    # still carries the words and the link.
    image_bytes: bytes | None = None
    if asset:
        try:
            image_bytes = Path(asset.inline_path).read_bytes()
        except OSError:
            log.warning(
                "send: inline image missing for asset %s (%s) — sending without it",
                asset.id, asset.inline_path,
            )
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
            db.rollback()   # committing again after a failed statement raises

    if wa_recipients and not wa_defer and wa_batch_running(event.id):
        # A scheduled send runs the WhatsApp half inline. Without this check it
        # could walk the same queued rows as a background batch already working
        # through them, and everyone in the overlap gets the invitation twice.
        log.warning(
            "whatsapp: a batch for event %s is already running; skipping the "
            "inline half", event.id,
        )
        return SendResult(
            sent=sent, failed=failed, mode=settings.send_mode.value,
            wa_pending=len(wa_recipients),
        )
    if wa_recipients and wa_defer:
        # Email is quick and stays inline; WhatsApp is paced and goes to a
        # background batch. Recipients stay 'queued' until it reaches them, so the
        # dashboard shows progress on any refresh.
        return SendResult(
            sent=sent, failed=failed, mode=settings.send_mode.value,
            wa_pending=len(wa_recipients),
        )
    if wa_recipients:
        with _wa_claim(event.id) as claimed:
            wa = (
                _send_whatsapp(
                    db, event, user, settings, wa_recipients, note=note,
                    card=image_bytes, card_mime=(asset.mime if asset else "image/jpeg"),
                )
                if claimed
                else _WaOutcome(0, 0, None)
            )
    else:
        wa = _WaOutcome(0, 0, None)
    return SendResult(
        sent=sent + wa.sent,
        failed=failed + wa.failed,
        mode=settings.send_mode.value,
        wa_sent=wa.sent,
        wa_failed=wa.failed,
        wa_blocked=wa.blocked,
    )


# WhatsApp batches run here rather than on the threadpool Starlette uses for
# sync route handlers. A paced batch holds its thread for minutes, and that pool
# serves every other page in the app — a few cards sending at once could stall the
# site for everyone. Two workers also caps how many batches run at once, which is
# its own kindness to a WhatsApp account.
_wa_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wa-batch")


def submit_whatsapp_batch(session_factory, event_id: str, settings: Settings):
    """Queue a batch on the WhatsApp pool and hand back its Future."""
    return _wa_pool.submit(send_whatsapp_batch, session_factory, event_id, settings)


# Events with a WhatsApp batch in flight. A single-process guard: the app runs as
# one uvicorn worker, and the alternative — a host pressing Send twice while a
# paced batch is still working through the list — means duplicate messages, which
# over WhatsApp is both rude and exactly the behaviour that gets accounts limited.
_wa_in_flight: set[str] = set()
_wa_lock = threading.Lock()


def wa_batch_running(event_id: str) -> bool:
    with _wa_lock:
        return event_id in _wa_in_flight


@contextlib.contextmanager
def _wa_claim(event_id: str):
    """Claim the right to send this event's WhatsApp half, or yield False.

    Both paths go through this: the background batch and the inline one a
    scheduled send uses. Anything that sends without claiming can double-message
    people, which is both rude and the burst WhatsApp restricts accounts for.
    """
    with _wa_lock:
        if event_id in _wa_in_flight:
            yield False
            return
        _wa_in_flight.add(event_id)
    try:
        yield True
    finally:
        with _wa_lock:
            _wa_in_flight.discard(event_id)


def send_whatsapp_batch(session_factory, event_id: str, settings: Settings) -> None:
    """Deliver an event's queued WhatsApp invitations, paced, in its own session.

    Written to be run off the request path (a Starlette BackgroundTask), so it
    takes a session factory rather than a session and never raises at the caller.
    """
    with _wa_claim(event_id) as claimed:
        if not claimed:
            log.info("whatsapp: a batch for event %s is already running", event_id)
            return
        _run_whatsapp_batch(session_factory, event_id, settings)


def _run_whatsapp_batch(session_factory, event_id: str, settings: Settings) -> None:
    db: Session | None = None
    try:
        # Inside the try: a pool timeout here would otherwise skip the finally
        # and leave the event permanently "sending", refusing every later batch.
        db = session_factory()
        event = db.get(Event, event_id)
        user = db.get(User, event.user_id) if event else None
        if event is None or user is None:
            return
        queued = db.execute(
            select(Recipient).where(
                Recipient.event_id == event.id, Recipient.status == "queued"
            )
        ).scalars().all()
        recipients = [r for r in queued if r.phone]
        if not recipients:
            return
        asset = db.get(Asset, event.asset_id) if event.asset_id else None
        card: bytes | None = None
        if asset:
            try:
                card = Path(asset.inline_path).read_bytes()
            except OSError:
                log.warning("whatsapp batch: inline image missing for asset %s", asset.id)
        out = _send_whatsapp(
            db, event, user, settings, recipients,
            card=card, card_mime=(asset.mime if asset else "image/jpeg"),
        )
        log.info(
            "whatsapp batch for event %s: sent=%d failed=%d blocked=%s",
            event_id, out.sent, out.failed, out.blocked,
        )
        # Reminders hang off sent_at, which only exists now that the batch has
        # run. The route schedules them for the email half; if we didn't do it
        # here, a WhatsApp guest would never be nudged.
        if out.sent:
            from kith.services import scheduler  # local: scheduler imports us

            scheduler.schedule_event_reminders(db, event, settings)
    except Exception:
        log.exception("whatsapp batch failed for event %s", event_id)
    finally:
        if db is not None:
            db.close()


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
    card: bytes | None = None,
    card_mime: str = "image/jpeg",
) -> _WaOutcome:
    """Send this event's WhatsApp invitations. Never raises — it reports.

    ``card`` is the inline copy of the card image, when there is one and it's
    still on disk (the retention sweep removes the heavy full-res copy, and older
    rows can be missing the inline one too). Without it this is a text message,
    which is exactly what a text-only card should be.
    """
    rsvp = bool((event.blocks or {}).get("rsvp"))
    base = settings.base_url.rstrip("/")
    host_name = user.display_name or "A friend"
    when = wamessage.when_line(event.event_date, event.event_time)
    invitation = eventkind.is_invitation(event.blocks, event.event_date)
    dry = settings.send_mode == SendMode.dry_run

    client = None
    session = user.wa_session or ""
    if not dry:
        # One pre-flight for the whole batch: re-reads the live session, and
        # refuses on not-linked / timelocked / capped before anything is sent.
        try:
            wa_link.sendable(db, user, settings)
        except waha.Timelocked as e:
            user.wa_timelock_until = e.ends_at or _far_future()
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
        session = user.wa_session or ""  # sendable() guarantees this is set

    sent = failed = 0
    for i, r in enumerate(recipients):
        # Re-read the row before using it. A paced batch holds this session for
        # minutes, and the factory keeps objects unexpired after commit, so
        # without this the batch works from a snapshot: it would message someone
        # the host removed mid-batch, and then die on the stale UPDATE.
        db.expire(r)
        try:
            still_queued = r.status == "queued"
        except Exception:      # the row was deleted under us
            log.info("whatsapp: recipient %s went away mid-batch; skipping", r.id)
            db.rollback()
            _pace(settings, dry, i, len(recipients))
            continue
        if not still_queued:
            log.info(
                "whatsapp: recipient %s is no longer queued (%s); skipping",
                r.id, r.status,
            )
            _pace(settings, dry, i, len(recipients))
            continue
        text = wamessage.invite_text(
            title=event.title,
            host_name=host_name,
            view_url=f"{base}/i/{r.token}",
            recipient_name=r.name,
            when=when,
            rsvp=rsvp,
            note=note,
            invitation=invitation,
        )
        to = user.wa_number if settings.send_mode == SendMode.self_only else r.phone
        if not to:
            # self-only with no number stored for the host: nothing to send to.
            # Better to report it than to hand WAHA an empty chat id.
            log.warning("whatsapp: no destination number for recipient %s", r.id)
            failed += 1
            _pace(settings, dry, i, len(recipients))
            continue
        try:
            if dry:
                _write_wa_outbox(
                    settings, event.id, r.id, to, text,
                    card_bytes=len(card) if card is not None else None,
                )
            elif client is not None:
                # Ask WhatsApp whether the number is really there first. Messaging
                # numbers that aren't on WhatsApp is one of the things that earns
                # an account a reachout timelock, so a wrong digit should cost one
                # recipient, not the host's ability to send at all. A check that
                # errors is not treated as an answer — we go ahead and send.
                chat_id = None
                try:
                    check = client.check_exists(session, to)
                    if not check.exists:
                        log.warning("whatsapp: %s is not on WhatsApp (event %s)", r.id, event.id)
                        failed += 1
                        _pace(settings, dry, i, len(recipients))
                        continue
                    chat_id = check.chat_id
                except waha.WahaError:
                    log.warning("whatsapp: existence check failed for %s; sending anyway", r.id)
                if card is not None:
                    # One message, not two: the card with the words as its
                    # caption, the way a person would send a photo.
                    res = client.send_image(
                        session, to, card, mimetype=card_mime, caption=text,
                        filename=f"{event.title or 'card'}.jpg"[:60], chat_id=chat_id,
                    )
                else:
                    res = client.send_text(session, to, text, chat_id=chat_id)
                r.wa_message_id = res.get("id")
            r.status = "sent"
            r.sent_at = datetime.now(UTC)
            sent += 1
            db.commit()
        except waha.Timelocked as e:
            # Hit mid-batch: stop. Retrying is what makes it worse, and the rest
            # stay queued so they go out once it lifts.
            user.wa_timelock_until = e.ends_at or _far_future()
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
            db.rollback()   # a failed statement poisons the session otherwise
            # A restriction arriving mid-batch does NOT surface as Timelocked from
            # a send call — WAHA answers with an error whose shape we don't
            # control, so error 463 looks like any other failure. Re-read the
            # session instead: if WhatsApp has restricted the account, stop now.
            # Grinding through the rest is precisely what deepens a timelock.
            if not dry and client is not None:
                try:
                    wa_link.sendable(db, user, settings)
                except waha.Timelocked as e:
                    user.wa_timelock_until = e.ends_at or _far_future()
                    db.commit()
                    log.warning(
                        "whatsapp: a send failed and the account is timelocked; "
                        "stopping with %d recipient(s) still queued",
                        len(recipients) - i - 1,
                    )
                    return _WaOutcome(sent, failed, "timelock")
                except waha.Capped:
                    return _WaOutcome(sent, failed, "capped")
                except waha.NotLinked:
                    return _WaOutcome(sent, failed, "not-linked")
                except waha.WahaError:
                    pass    # can't tell; treat it as this one recipient's problem
        _pace(settings, dry, i, len(recipients))

    return _WaOutcome(sent, failed, None)
