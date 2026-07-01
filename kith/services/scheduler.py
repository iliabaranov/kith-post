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
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kith.config import SendMode, Settings
from kith.core import mailbuild
from kith.core import reminders as rem
from kith.db.models import Event, Recipient, Reminder, User

log = logging.getLogger("kith")


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
    except Exception:
        log.exception("reminder send failed (reminder %s)", reminder.id)
        reminder.status, reminder.sent_at = "pending", None
        db.commit()
        return False
    return True


@dataclass
class SweepResult:
    considered: int
    sent: int
    skipped: int


def sweep_tick(session_factory, settings: Settings, *, now: datetime | None = None) -> SweepResult:
    """Fire every pending reminder whose scheduled_for has passed. Own DB session."""
    now = now or datetime.now(UTC)
    db: Session = session_factory()
    try:
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
        return SweepResult(considered=len(due), sent=sent, skipped=skipped)
    finally:
        db.close()


async def sweep_loop(app, settings: Settings, interval_s: int) -> None:
    """Run sweep_tick every interval_s. DB work is sync, so offload to a thread."""
    while True:
        try:
            await asyncio.to_thread(sweep_tick, app.state.session_factory, settings)
        except Exception:
            log.exception("reminder sweep failed")
        await asyncio.sleep(interval_s)
