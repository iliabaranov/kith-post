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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kith.config import SendMode, Settings
from kith.core import eventkind, mailbuild, phones, smsmessage, wamessage
from kith.core.channels import (
    ALL_CHANNELS,
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    CHANNEL_WHATSAPP,
    channel_of,
)
from kith.db.models import Asset, Event, Recipient, SmsOptOutEvent, User
from kith.services import sms as sms_channel
from kith.services import sms_link, waha
from kith.services import wa_session as wa_link
from kith.services.contacts import phone_hash
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


def _pace(
    settings: Settings, dry: bool, index: int, total: int,
    channel: str = CHANNEL_WHATSAPP,
) -> None:
    """Wait before the next send. A burst is what WhatsApp restricts accounts
    for and what gets a text spam-filtered, and an exactly-even cadence is its
    own signature — neither looks like a person working through a list.

    Called from the skip paths too: a list with several bad numbers would
    otherwise fire its existence checks back to back, which is the same burst.
    """
    if dry or index + 1 >= total:
        return
    gap = next_send_gap(settings, channel=channel)
    if gap > 0:
        log.info("%s: pausing %.1fs before the next send", channel, gap)
        time.sleep(gap)


def next_send_gap(
    settings: Settings, rng: random.Random | None = None,
    *, channel: str = CHANNEL_WHATSAPP,
) -> float:
    """A fresh random pause, in seconds, to put between two sends on a channel.

    Uniform over that channel's configured range. The randomness is the point: a
    fixed gap is as machine-like as no gap at all, only slower. SMS gets its own,
    shorter range — a carrier throttles where WhatsApp bans — and the default
    stays WhatsApp so existing callers keep the behaviour they had.
    """
    if channel == CHANNEL_SMS:
        lo = max(0.0, settings.sms_send_gap_min_seconds)
        hi = max(lo, settings.sms_send_gap_max_seconds)
    else:
        lo = max(0.0, settings.waha_send_gap_min_seconds)
        hi = max(lo, settings.waha_send_gap_max_seconds)
    if hi <= 0:
        return 0.0
    return (rng or random).uniform(lo, hi)


@dataclass
class SendResult:
    """Totals across every channel, plus what a channel refused to do.

    ``sent`` and ``failed`` are the totals; the per-channel fields break them
    down so the dashboard can say which half of a mixed send is still going.
    """

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
    sms_sent: int = 0
    sms_failed: int = 0
    # Paced and handed to a background batch, exactly like the WhatsApp half.
    sms_pending: int = 0
    # Set when the SMS batch was stopped as a whole rather than per recipient:
    # no provider configured, or the provider rejected our credentials. Retrying
    # the remaining recipients against either of those is pure waste.
    sms_blocked: str | None = None


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


def opted_out_hashes(db: Session) -> frozenset[str]:
    """Blind indexes of every number whose latest reply was STOP.

    Instance-wide, not per user: the site texts from one number, so a STOP is a
    decision about that number and it binds every host here. Read from the
    opt-out log rather than from any flag on a contact or recipient — those rows
    are the host's to delete, and an opt-out has to outlive both the card it
    arrived on and the address-book entry it might be filed under. The set is
    small: it only ever holds people who asked to be left alone.
    """
    latest = (
        select(SmsOptOutEvent.phone_hash, func.max(SmsOptOutEvent.id).label("last"))
        .group_by(SmsOptOutEvent.phone_hash)
        .subquery()
    )
    rows = db.execute(
        select(SmsOptOutEvent.phone_hash)
        .join(latest, SmsOptOutEvent.id == latest.c.last)
        .where(SmsOptOutEvent.kind == "stop")
    ).scalars().all()
    return frozenset(rows)


def is_opted_out(r: Recipient, opted_out: frozenset[str] | set[str]) -> bool:
    """Whether this recipient must not be texted."""
    number = r.phone
    return bool(number) and phone_hash(number or "") in opted_out


def _write_sms_outbox(
    settings: Settings, event_id: str, recipient_id: str, to: str, text: str,
) -> None:
    """dry-run's SMS equivalent of an .eml — the exact text, who to, and the cost.

    The segment count is written alongside because it is the one thing about a
    text that cannot be judged by reading it, and dry-run is where a host is
    supposed to find that out.
    """
    d = settings.outbox_dir / event_id / "sms"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{recipient_id}.txt").write_text(
        f"To: {to}\nSegments: {smsmessage.segments(text)}\n\n{text}\n"
    )


def send_event(
    db: Session, event: Event, user: User, settings: Settings, *,
    note: str | None = None, wa_defer: bool = False, sms_defer: bool = False,
) -> SendResult:
    queued = db.execute(
        select(Recipient).where(Recipient.event_id == event.id, Recipient.status == "queued")
    ).scalars().all()
    # A recipient is on exactly one channel, and the row says which. Asking
    # channel_of rather than "is phone set" is what lets a second phone-based
    # channel exist without this split quietly misrouting it.
    recipients = [r for r in queued if channel_of(r) == CHANNEL_EMAIL]
    wa_recipients = [r for r in queued if channel_of(r) == CHANNEL_WHATSAPP]
    sms_recipients = [r for r in queued if channel_of(r) == CHANNEL_SMS]
    for r in queued:
        if channel_of(r) not in ALL_CHANNELS:
            # A hand-edited row, or one written by a newer build. It lands in no
            # list, so it would sit 'queued' for ever with nothing said; say so.
            log.warning(
                "send: recipient %s is on unknown channel %r; leaving it queued",
                r.id, r.channel,
            )
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
        sms = _run_sms_half(
            db, event, user, settings, sms_recipients, note=note, defer=sms_defer,
        )
        return SendResult(
            sent=sent + sms.sent, failed=failed + sms.failed,
            mode=settings.send_mode.value,
            wa_pending=len(wa_recipients),
            sms_sent=sms.sent, sms_failed=sms.failed,
            sms_pending=sms.pending, sms_blocked=sms.blocked,
        )
    if wa_recipients:
        # Mark the work owed before anything is sent, so a crash between here and
        # the last message leaves a record the sweep can act on.
        event.wa_batch_started_at = datetime.now(UTC)
        db.commit()
    if wa_recipients and wa_defer:
        # Email is quick and stays inline; WhatsApp is paced and goes to a
        # background batch. Recipients stay 'queued' until it reaches them, so the
        # dashboard shows progress on any refresh.
        sms = _run_sms_half(
            db, event, user, settings, sms_recipients, note=note, defer=sms_defer,
        )
        return SendResult(
            sent=sent + sms.sent, failed=failed + sms.failed,
            mode=settings.send_mode.value,
            wa_pending=len(wa_recipients),
            sms_sent=sms.sent, sms_failed=sms.failed,
            sms_pending=sms.pending, sms_blocked=sms.blocked,
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
    sms = _run_sms_half(
        db, event, user, settings, sms_recipients, note=note, defer=sms_defer,
    )
    return SendResult(
        sent=sent + wa.sent + sms.sent,
        failed=failed + wa.failed + sms.failed,
        mode=settings.send_mode.value,
        wa_sent=wa.sent,
        wa_failed=wa.failed,
        wa_blocked=wa.blocked,
        sms_sent=sms.sent,
        sms_failed=sms.failed,
        sms_pending=sms.pending,
        sms_blocked=sms.blocked,
    )


@dataclass
class _SmsHalf:
    sent: int
    failed: int
    pending: int
    blocked: str | None


def _run_sms_half(
    db: Session, event: Event, user: User, settings: Settings,
    recipients: list[Recipient], *, note: str | None, defer: bool,
) -> _SmsHalf:
    """The SMS third of a send: defer it, run it, or stand aside for a batch.

    Pulled out of send_event because there are three exits from it and the SMS
    half has to be handled identically at each one — an early return that
    forgot it would silently drop every text.
    """
    if not recipients:
        return _SmsHalf(0, 0, 0, None)
    if defer:
        return _SmsHalf(0, 0, len(recipients), None)
    if sms_batch_running(event.id):
        # A scheduled send runs its channels inline. Without this check it could
        # walk the same queued rows as a background batch already working
        # through them, and everyone in the overlap gets the invitation twice.
        log.warning(
            "sms: a batch for event %s is already running; skipping the inline "
            "half", event.id,
        )
        return _SmsHalf(0, 0, len(recipients), None)
    with _claim(event.id, CHANNEL_SMS) as claimed:
        if not claimed:
            return _SmsHalf(0, 0, len(recipients), None)
        out = _send_sms(db, event, user, settings, recipients, note=note)
    return _SmsHalf(out.sent, out.failed, 0, out.blocked)


# Paced batches run here rather than on the threadpool Starlette uses for sync
# route handlers. A paced batch holds its thread for minutes, and that pool
# serves every other page in the app — a few cards sending at once could stall the
# site for everyone. Two workers also caps how many batches run at once, which is
# its own kindness to a WhatsApp account.
_batch_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="send-batch")


_batch_futures: set = set()


def _submit_batch(fn, session_factory, event_id: str, settings: Settings):
    fut = _batch_pool.submit(fn, session_factory, event_id, settings)
    # Tracked so callers that need to know when the work is done — tests, and a
    # graceful shutdown — have something to wait on. Discarded on completion so
    # the set stays the size of what's actually outstanding.
    _batch_futures.add(fut)
    fut.add_done_callback(_batch_futures.discard)
    return fut


def submit_whatsapp_batch(session_factory, event_id: str, settings: Settings):
    """Queue a WhatsApp batch on the send pool and hand back its Future."""
    return _submit_batch(send_whatsapp_batch, session_factory, event_id, settings)


def submit_sms_batch(session_factory, event_id: str, settings: Settings):
    """Queue an SMS batch on the send pool and hand back its Future."""
    return _submit_batch(send_sms_batch, session_factory, event_id, settings)


def wait_for_batches(timeout: float = 30.0) -> bool:
    """Block until every queued batch has finished. True if they all did."""
    from concurrent.futures import wait

    pending = set(_batch_futures)
    if not pending:
        return True
    done, not_done = wait(pending, timeout=timeout)
    return not not_done


# (event, channel) pairs with a paced batch in flight. A single-process guard:
# the app runs as one uvicorn worker, and the alternative — a host pressing Send
# twice while a paced batch is still working through the list — means duplicate
# messages, which over WhatsApp is both rude and exactly the behaviour that gets
# accounts limited, and over SMS is rude and billed twice.
#
# Keyed by channel, not by event alone: an event's WhatsApp and SMS halves walk
# disjoint sets of recipients, so one must not lock the other out.
_in_flight: set[tuple[str, str]] = set()
_batch_lock = threading.Lock()


def batch_running(event_id: str, channel: str) -> bool:
    with _batch_lock:
        return (event_id, channel) in _in_flight


def wa_batch_running(event_id: str) -> bool:
    return batch_running(event_id, CHANNEL_WHATSAPP)


def sms_batch_running(event_id: str) -> bool:
    return batch_running(event_id, CHANNEL_SMS)


@contextlib.contextmanager
def _claim(event_id: str, channel: str):
    """Claim the right to send this event's half of a channel, or yield False.

    Every path goes through this: the background batch and the inline one a
    scheduled send uses. Anything that sends without claiming can double-message
    people, which is both rude and the burst carriers filter on.
    """
    key = (event_id, channel)
    with _batch_lock:
        if key in _in_flight:
            yield False
            return
        _in_flight.add(key)
    try:
        yield True
    finally:
        with _batch_lock:
            _in_flight.discard(key)


def _wa_claim(event_id: str):
    return _claim(event_id, CHANNEL_WHATSAPP)


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
        recipients = [r for r in queued if channel_of(r) == CHANNEL_WHATSAPP]
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


def send_sms_batch(session_factory, event_id: str, settings: Settings) -> None:
    """Deliver an event's queued SMS invitations, paced, in its own session.

    Written to be run off the request path (a Starlette BackgroundTask), so it
    takes a session factory rather than a session and never raises at the caller.
    """
    with _claim(event_id, CHANNEL_SMS) as claimed:
        if not claimed:
            log.info("sms: a batch for event %s is already running", event_id)
            return
        _run_sms_batch(session_factory, event_id, settings)


def _run_sms_batch(session_factory, event_id: str, settings: Settings) -> None:
    db: Session | None = None
    try:
        # Inside the try: a pool timeout here would otherwise skip the finally
        # and leave the session open.
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
        recipients = [r for r in queued if channel_of(r) == CHANNEL_SMS]
        if not recipients:
            return
        out = _send_sms(db, event, user, settings, recipients)
        log.info(
            "sms batch for event %s: sent=%d failed=%d blocked=%s",
            event_id, out.sent, out.failed, out.blocked,
        )
        # Reminders hang off sent_at, which only exists now that the batch has
        # run. The route schedules them for the email half; without this an SMS
        # guest would never be nudged.
        if out.sent:
            from kith.services import scheduler  # local: scheduler imports us

            scheduler.schedule_event_reminders(db, event, settings)
    except Exception:
        log.exception("sms batch failed for event %s", event_id)
    finally:
        if db is not None:
            db.close()


@dataclass
class _WaOutcome:
    sent: int
    failed: int
    blocked: str | None


# Why the last SMS batch for a host stopped, if it did. A stop is a fact about
# that host's provider — every card of theirs with queued texts is stuck for the
# same reason — and the batch runs after the response, so the redirect cannot
# carry it. This is how the dashboard learns it on a later load. Keyed by host,
# because hosts can text through different providers and one host's bad
# password is not another's problem. Cleared by the next text that goes
# through. In memory only: a restart is a fine moment to try again.
_sms_block_lock = threading.Lock()
_sms_last_block: dict[str, str] = {}


def sms_last_block(user_id: str) -> str | None:
    """The reason this host's last SMS batch stopped, or None if texts go out."""
    with _sms_block_lock:
        return _sms_last_block.get(user_id)


def _remember_sms_block(user_id: str, reason: str | None) -> None:
    with _sms_block_lock:
        if reason is None:
            _sms_last_block.pop(user_id, None)
        else:
            _sms_last_block[user_id] = reason


def _stopped(user_id: str, sent: int, failed: int, reason: str) -> _SmsOutcome:
    """A batch that stopped as a whole: record why, for the host's cards to see."""
    _remember_sms_block(user_id, reason)
    return _SmsOutcome(sent, failed, reason)


@dataclass
class _SmsOutcome:
    sent: int
    failed: int
    blocked: str | None


def _send_sms(
    db: Session,
    event: Event,
    user: User,
    settings: Settings,
    recipients: list[Recipient],
    *,
    note: str | None = None,
) -> _SmsOutcome:
    """Send this event's SMS invitations. Never raises — it reports.

    Text only, and no card: SMS carries no image, and the invitation page is
    where the picture lives. Otherwise this is the WhatsApp path with the
    WhatsApp-specific worries removed — there is no session to pre-flight, no
    existence check, no reachout timelock and no new-chat quota. What replaces
    them is smaller: a provider that is missing or rejects our credentials, both
    of which stop the whole batch because retrying either is pure waste.
    """
    rsvp = bool((event.blocks or {}).get("rsvp"))
    base = settings.base_url.rstrip("/")
    host_name = user.display_name or "A friend"
    when = smsmessage.when_line(event.event_date, event.event_time)
    invitation = eventkind.is_invitation(event.blocks, event.event_date)
    dry = settings.send_mode == SendMode.dry_run

    # Whose texting: the host's own settings when they have them, else the
    # site's. Resolved once for the batch.
    config = sms_link.config_for(db, user, settings)

    # self-only sends to the host's own test number, never to the guest. With
    # no usable number there is nowhere safe to send, so the batch stops with
    # every recipient still queued and says why. Falling through to the guest
    # is the one thing self-only exists to prevent — and marking the rows sent
    # for a text nobody got would be the same lie in a quieter voice.
    use_self = settings.send_mode == SendMode.self_only
    self_number = phones.normalize((config.self_number if config else "") or "")
    if use_self and not self_number:
        log.warning(
            "sms: self-only has no usable test number for host %s, so event %s is "
            "held with %d recipient(s) still queued", user.id, event.id, len(recipients),
        )
        return _stopped(user.id, 0, 0, "no-self-number")

    provider = None if dry else sms_channel.provider_from(config)
    # Read once for the batch. Enforced even in dry-run: an operator checking
    # the outbox should see the same set of texts a live send would produce, and
    # a dry-run that quietly includes an opted-out number is a dry run that
    # hides the bug.
    opted_out = opted_out_hashes(db)

    sent = failed = 0
    for i, r in enumerate(recipients):
        # Re-read the row before using it. A paced batch holds this session for
        # minutes, and the factory keeps objects unexpired after commit, so
        # without this the batch works from a snapshot: it would message someone
        # the host removed mid-batch, and then die on the stale UPDATE.
        rid = r.id   # read before expiring: after a delete, even the id won't load
        db.expire(r)
        try:
            still_queued = r.status == "queued"
        except Exception:      # the row was deleted under us
            log.info("sms: recipient %s went away mid-batch; skipping", rid)
            db.rollback()
            _pace(settings, dry, i, len(recipients), CHANNEL_SMS)
            continue
        if not still_queued:
            log.info("sms: recipient %s is no longer queued (%s); skipping", r.id, r.status)
            _pace(settings, dry, i, len(recipients), CHANNEL_SMS)
            continue
        if is_opted_out(r, opted_out):
            # Left 'queued' rather than marked failed or sent: neither is true.
            # Nothing will retry it, because every later send re-checks this.
            log.info(
                "sms: recipient %s replied STOP; not texting them again", r.id
            )
            continue
        text = smsmessage.invite_text(
            title=event.title,
            host_name=host_name,
            view_url=f"{base}/i/{r.token}",
            recipient_name=r.name,
            when=when,
            rsvp=rsvp,
            note=note,
            invitation=invitation,
        )
        to = self_number if use_self else r.phone
        if not to:
            # An SMS row with no number is a broken row, not a send.
            log.warning("sms: no destination number for recipient %s", r.id)
            failed += 1
            _pace(settings, dry, i, len(recipients), CHANNEL_SMS)
            continue
        try:
            if dry:
                _write_sms_outbox(settings, event.id, r.id, to, text)
            elif provider is not None:
                res = provider.send(to, text)
                r.sms_message_id = res.message_id
            r.status = "sent"
            r.sent_at = datetime.now(UTC)
            sent += 1
            db.commit()
        except sms_channel.SmsNotConfigured:
            # Live mode with no provider. Every remaining recipient would fail
            # the same way, and they stay queued for when it is configured.
            db.rollback()
            log.warning(
                "sms: no provider configured; stopping with %d recipient(s) "
                "still queued", len(recipients) - i,
            )
            return _stopped(user.id, sent, failed, "not-configured")
        except sms_channel.SmsAuthError:
            db.rollback()
            log.warning(
                "sms: the provider rejected our credentials; stopping with %d "
                "recipient(s) still queued", len(recipients) - i,
            )
            return _stopped(user.id, sent, failed, "auth")
        except sms_channel.SmsMisconfigured as e:
            # Ours to fix, not this recipient's fault — and the same for everyone
            # after them, so one paced call is enough to learn it.
            db.rollback()
            log.warning(
                "sms: the provider rejected our configuration (%s); stopping "
                "with %d recipient(s) still queued", e, len(recipients) - i,
            )
            return _stopped(user.id, sent, failed, "misconfigured")
        except sms_channel.SmsRateLimited as e:
            # Pressing on would only be refused faster. Stop; the host re-sends.
            db.rollback()
            log.warning(
                "sms: the provider asked us to slow down (%s); stopping with %d "
                "recipient(s) still queued", e, len(recipients) - i,
            )
            return _stopped(user.id, sent, failed, "rate-limited")
        except Exception:
            log.exception("sms send failed for recipient %s (event %s)", r.id, event.id)
            failed += 1  # left 'queued' so a retry can pick it up
            db.rollback()   # a failed statement poisons the session otherwise
        _pace(settings, dry, i, len(recipients), CHANNEL_SMS)

    if sent:
        _remember_sms_block(user.id, None)  # texts are going out again
    return _SmsOutcome(sent, failed, None)


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
        rid = r.id   # read before expiring: after a delete, even the id won't load
        db.expire(r)
        try:
            still_queued = r.status == "queued"
        except Exception:      # the row was deleted under us
            log.info("whatsapp: recipient %s went away mid-batch; skipping", rid)
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

    # A full pass finished. Whatever is still queued failed for its own reasons —
    # a wrong number won't come right by trying again — so release the job rather
    # than have the sweep retry it forever. A batch stopped by a restriction
    # returns earlier and deliberately leaves the marker set.
    event.wa_batch_started_at = None
    db.commit()
    return _WaOutcome(sent, failed, None)
