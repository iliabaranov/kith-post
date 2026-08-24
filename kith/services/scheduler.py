"""Reminder scheduling + the sweep worker (G5, §8).

The sweep is an in-process background task (started in the app lifespan) that fires
any pending Reminder whose time has passed. Reminders are persisted rows, so this is
downtime-safe: overdue rows simply go out on the next tick. ``sweep_tick`` is pure of
the loop and takes an injected ``now`` so the whole thing is testable in dry-run.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from kith.config import SendMode, Settings
from kith.core import mailbuild, wamessage
from kith.core import reminders as rem
from kith.db.models import Asset, Event, Recipient, Reminder, User
from kith.services import wa_session as wa_link
from kith.services import waha
from kith.services.gmail import GmailAuthError

log = logging.getLogger("kith")


def _send_wa_reminder(
    db: Session, reminder: Reminder, r: Recipient, ev: Event, user: User,
    settings: Settings, *, rsvp: bool,
) -> bool:
    """A nudge in the same WhatsApp chat as the invitation.

    Same shape as the email path: marked sent before the network call so a crash
    can't re-send, reverted to pending on a transient failure so the next tick
    retries. A restriction from WhatsApp reverts it too — the nudge isn't lost,
    it just waits, and hammering a timelocked account is what makes it worse.
    """
    text = wamessage.reminder_text(
        title=ev.title,
        host_name=user.display_name or "A friend",
        view_url=f"{settings.base_url.rstrip('/')}/i/{r.token}",
        recipient_name=r.name,
        when=wamessage.when_line(ev.event_date, ev.event_time),
        rsvp=rsvp,
    )
    reminder.status, reminder.sent_at = "sent", datetime.now(UTC)
    db.commit()
    try:
        if settings.send_mode == SendMode.dry_run:
            d = settings.outbox_dir / ev.id / "whatsapp" / "reminders"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{reminder.id}.txt").write_text(f"To: {r.phone}\n\n{text}\n")
        else:
            wa_link.sendable(db, user, settings)  # re-reads the live session
            to = user.wa_number if settings.send_mode == SendMode.self_only else r.phone
            if not to:
                raise waha.WahaError(f"no destination number for recipient {r.id}")
            wa_link.client(settings).send_text(user.wa_session or "", to, text)
    except waha.Timelocked as e:
        user.wa_timelock_until = e.ends_at
        reminder.status, reminder.sent_at = "pending", None
        db.commit()
        log.warning("reminder: WhatsApp timelock active for user %s; holding", user.id)
        return False
    except waha.WahaError:
        reminder.status, reminder.sent_at = "pending", None
        db.commit()
        log.warning("reminder: WhatsApp unavailable for user %s; will retry", user.id)
        return False
    except Exception:
        log.exception("reminder: WhatsApp send failed (reminder %s)", reminder.id)
        reminder.status, reminder.sent_at = "pending", None
        db.commit()
        return False
    return True


def resolved_cfg(settings: Settings, event: Event) -> rem.ReminderConfig:
    """Global reminder config (added in P5) with the event's per-event override on top.
    Falls back to defaults if global config isn't wired yet."""
    base = getattr(settings, "reminders", None)
    return rem.resolve_reminder_config(base, event.reminder_cfg)


def _as_utc(dt: datetime) -> datetime:
    """SQLite may hand back naive datetimes — treat them as UTC for comparison."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _write_reminder_outbox(settings: Settings, event_id: str, reminder_id: str, msg) -> None:
    d = settings.outbox_dir / event_id / "reminders"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{reminder_id}.eml").write_bytes(msg.as_bytes())


def send_one_reminder(db: Session, reminder: Reminder, settings: Settings) -> bool:
    """Fire a single due reminder. Re-checks eligibility at send time (downtime-safe),
    honors send_mode, threads under the original via Gmail threadId, and flips state
    atomically. Returns True if actually sent."""
    r = db.get(Recipient, reminder.recipient_id)
    ev = db.get(Event, reminder.event_id)
    user = db.get(User, ev.user_id) if ev else None
    if r is None or ev is None or user is None:
        reminder.status, reminder.skip_reason = "canceled", "missing"
        db.commit()
        return False

    cfg = resolved_cfg(settings, ev)
    if not rem.still_needs_nudge(r.status, r.first_open_at, cfg.target):
        reminder.status, reminder.skip_reason = "skipped", "engaged"
        db.commit()
        return False
    if ev.event_date and ev.event_date < date.today():
        reminder.status, reminder.skip_reason = "skipped", "after_event"
        db.commit()
        return False
    already_sent = db.execute(
        select(func.count()).select_from(Reminder).where(
            Reminder.recipient_id == r.id, Reminder.status == "sent"
        )
    ).scalar_one()
    if already_sent >= cfg.max_per_recipient:
        reminder.status, reminder.skip_reason = "skipped", "capped"
        db.commit()
        return False

    rsvp = bool((ev.blocks or {}).get("rsvp"))
    host_name = user.display_name or "A friend"
    view_url = f"{settings.base_url.rstrip('/')}/i/{r.token}"
    if r.phone:
        return _send_wa_reminder(db, reminder, r, ev, user, settings, rsvp=rsvp)
    msg = mailbuild.build_email(
        subject=mailbuild.reminder_subject(mailbuild.subject_for(ev.title, rsvp)),
        from_name=user.display_name, from_email=user.email,
        to_email=(user.email if settings.send_mode == SendMode.self_only else r.email),
        to_name=r.name,
        html=mailbuild.reminder_html(
            title=ev.title, host_name=host_name, view_url=view_url, rsvp=rsvp
        ),
        text=mailbuild.reminder_text(
            title=ev.title, host_name=host_name, view_url=view_url, rsvp=rsvp
        ),
        message_id=mailbuild.make_message_id(user.email.rsplit("@", 1)[-1] or "kith.post"),
        in_reply_to=r.rfc822_message_id,   # thread under the original (with threadId)
        references=r.rfc822_message_id,
    )
    # Mark sent BEFORE the network call so a hard crash never re-sends; revert to
    # pending on a transient failure so the next tick retries.
    reminder.status, reminder.sent_at = "sent", datetime.now(UTC)
    db.commit()
    try:
        if settings.send_mode == SendMode.dry_run:
            _write_reminder_outbox(settings, ev.id, reminder.id, msg)
        else:
            from kith.services import gmail

            gmail.gmail_send(
                settings, user.refresh_token, mailbuild.to_raw(msg), thread_id=r.thread_id
            )
    except GmailAuthError:
        user.reconnect_needed = True  # expired/revoked token — prompt a reconnect
        reminder.status, reminder.sent_at = "pending", None
        db.commit()
        log.warning("reminder: Google connection invalid for user %s", user.id)
        return False
    except Exception:
        log.exception("reminder send failed (reminder %s)", reminder.id)
        reminder.status, reminder.sent_at = "pending", None
        db.commit()
        return False
    return True


def purge_expired_assets(db: Session, settings: Settings, *, now: datetime | None = None) -> int:
    """Delete heavy full-res card images past their retention window (measured from
    the event date, or creation for dateless/orphaned cards). Keeps the small inline
    copy and the DB row (marked purged_at). Returns how many were purged."""
    days = getattr(settings, "asset_retention_days", 0) or 0
    if days <= 0:
        return 0
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=days)).date()
    purged = 0
    for a in db.execute(select(Asset).where(Asset.purged_at.is_(None))).scalars().all():
        ev = db.execute(select(Event).where(Event.asset_id == a.id)).scalars().first()
        if ev is not None and ev.event_date is not None:
            basis = ev.event_date
        elif ev is not None and ev.created_at is not None:
            basis = _as_utc(ev.created_at).date()
        else:  # orphaned (event deleted) or missing timestamp
            basis = _as_utc(a.created_at).date() if a.created_at is not None else cutoff
        if basis >= cutoff:
            continue
        try:
            p = Path(a.full_path)
            if p.exists():
                p.unlink()
        except Exception:
            log.exception("purge: could not remove %s", a.full_path)
        a.purged_at = now
        purged += 1
    db.commit()
    return purged


@dataclass
class SweepResult:
    considered: int
    sent: int
    skipped: int
    purged: int = 0
    scheduled: int = 0


def send_due_scheduled(db: Session, settings: Settings, *, now: datetime | None = None) -> int:
    """Fire any card whose scheduled_send_at has passed. send_event only touches
    still-queued recipients and is idempotent, so a partial/failed send just
    keeps the schedule and retries next tick (e.g. after a Google reconnect).
    The schedule is cleared once every recipient has gone out."""
    from kith.services import send  # local import avoids an import cycle

    now = now or datetime.now(UTC)
    fired = 0
    events = db.execute(
        select(Event).where(Event.scheduled_send_at.is_not(None))
    ).scalars().all()
    for ev in events:
        if _as_utc(ev.scheduled_send_at) > now:
            continue
        user = db.get(User, ev.user_id)
        if user is None:
            ev.scheduled_send_at = None
            db.commit()
            continue
        # Can't send email live without a token — keep the schedule and retry
        # later. Only holds the card back if it actually has email recipients; a
        # WhatsApp-only card doesn't need Google at all.
        if settings.send_mode != SendMode.dry_run and not user.refresh_token:
            wants_email = db.execute(
                select(Recipient).where(
                    Recipient.event_id == ev.id, Recipient.status == "queued"
                )
            ).scalars().all()
            if any(not r.phone for r in wants_email):
                continue
        queued = db.execute(
            select(func.count()).select_from(Recipient).where(
                Recipient.event_id == ev.id, Recipient.status == "queued"
            )
        ).scalar_one()
        if queued == 0:
            ev.scheduled_send_at = None
            db.commit()
            continue
        send.send_event(db, ev, user, settings)
        schedule_event_reminders(db, ev, settings)
        remaining = db.execute(
            select(func.count()).select_from(Recipient).where(
                Recipient.event_id == ev.id, Recipient.status == "queued"
            )
        ).scalar_one()
        if remaining == 0:
            ev.scheduled_send_at = None
        db.commit()
        fired += 1
    return fired


def sweep_tick(session_factory, settings: Settings, *, now: datetime | None = None) -> SweepResult:
    """Periodic maintenance: fire due scheduled sends + reminders, then purge assets."""
    now = now or datetime.now(UTC)
    db: Session = session_factory()
    try:
        scheduled = send_due_scheduled(db, settings, now=now)
        pending = db.execute(
            select(Reminder).where(Reminder.status == "pending").order_by(Reminder.scheduled_for)
        ).scalars().all()
        due = [r for r in pending if _as_utc(r.scheduled_for) <= now]
        sent = skipped = 0
        for reminder in due:
            if send_one_reminder(db, reminder, settings):
                sent += 1
            else:
                skipped += 1
        purged = purge_expired_assets(db, settings, now=now)
        return SweepResult(
            considered=len(due), sent=sent, skipped=skipped, purged=purged, scheduled=scheduled
        )
    finally:
        db.close()


def schedule_recipient_reminders(
    db: Session, event: Event, recipient: Recipient, cfg: rem.ReminderConfig,
    *, now: datetime | None = None,
) -> int:
    """Rebuild the pending reminders for one recipient (idempotent: drops existing
    pending first). No-op unless reminders are enabled, the event is dated with an
    RSVP block, the recipient has been sent, and they still need a nudge."""
    db.execute(
        delete(Reminder).where(
            Reminder.recipient_id == recipient.id, Reminder.status == "pending"
        )
    )
    ok = (
        cfg.enabled
        and event.event_date is not None
        and bool((event.blocks or {}).get("rsvp"))
        and recipient.sent_at is not None
        and rem.still_needs_nudge(recipient.status, recipient.first_open_at, cfg.target)
    )
    slots = []
    if ok:
        slots = rem.compute_slots(
            sent_at=_as_utc(recipient.sent_at), event_date=event.event_date,
            event_time=event.event_time, tz=event.timezone, cfg=cfg,
            now=now or datetime.now(UTC),
        )
        for s in slots:
            db.add(Reminder(
                event_id=event.id, recipient_id=recipient.id,
                scheduled_for=s.scheduled_for, offset_label=s.label, status="pending",
            ))
    db.commit()
    return len(slots)


def schedule_event_reminders(
    db: Session, event: Event, settings: Settings, *, now: datetime | None = None
) -> int:
    """(Re)build reminders for every already-sent recipient of an event."""
    cfg = resolved_cfg(settings, event)
    sent_recipients = db.execute(
        select(Recipient).where(Recipient.event_id == event.id, Recipient.status == "sent")
    ).scalars().all()
    return sum(schedule_recipient_reminders(db, event, r, cfg, now=now) for r in sent_recipients)


def cancel_pending_reminders(db: Session, recipient_id: str, reason: str = "engaged") -> None:
    """Stop future nudges for one recipient (they engaged)."""
    db.execute(
        update(Reminder)
        .where(Reminder.recipient_id == recipient_id, Reminder.status == "pending")
        .values(status="canceled", skip_reason=reason)
    )
    db.commit()


def cancel_all_pending_for_event(db: Session, event_id: str, reason: str = "rescheduled") -> None:
    db.execute(
        update(Reminder)
        .where(Reminder.event_id == event_id, Reminder.status == "pending")
        .values(status="canceled", skip_reason=reason)
    )
    db.commit()


async def sweep_loop(app, settings: Settings, interval_s: int) -> None:
    """Run sweep_tick every interval_s. DB work is sync, so offload to a thread."""
    while True:
        try:
            await asyncio.to_thread(sweep_tick, app.state.session_factory, settings)
        except Exception:
            log.exception("reminder sweep failed")
        await asyncio.sleep(interval_s)
