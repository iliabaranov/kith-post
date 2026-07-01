"""Reminder scheduling — pure, testable, no DB (G5, §8).

Computes when to nudge a non-responding recipient, relative to the event date and
the moment their invite was sent. All datetimes in/out are timezone-aware UTC; the
caller injects ``now`` so the whole schedule is deterministic under test.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from datetime import date as Date
from zoneinfo import ZoneInfo

from kith.core import calendar as cal

_ND_RE = re.compile(r"^(\d+)d$")  # "7d", "3d", ...


@dataclass(frozen=True)
class ReminderConfig:
    enabled: bool = True
    target: str = "no-rsvp"                       # "no-rsvp" | "not-clicked"
    offsets: tuple[str, ...] = ("halfway", "7d", "3d")
    send_hour_local: int = 9                       # ~9am sender-local, not the exact instant
    min_gap_hours: int = 24                        # merge reminders closer than this
    max_per_recipient: int = 3


DEFAULTS = ReminderConfig()


@dataclass(frozen=True)
class Slot:
    scheduled_for: datetime   # aware UTC
    label: str


def resolve_reminder_config(
    base: ReminderConfig | None, override: Mapping | None
) -> ReminderConfig:
    """Per-event override (Event.reminder_cfg) merged over a base config."""
    base = base or DEFAULTS
    o = dict(override or {})
    return ReminderConfig(
        enabled=o.get("enabled", base.enabled),
        target=o.get("target", base.target),
        offsets=tuple(o.get("offsets", base.offsets)),
        send_hour_local=o.get("send_hour_local", base.send_hour_local),
        min_gap_hours=o.get("min_gap_hours", base.min_gap_hours),
        max_per_recipient=o.get("max_per_recipient", base.max_per_recipient),
    )


def event_moment(event_date: Date, event_time: str | None, tz: str | None) -> datetime:
    """The 'never send after this' deadline as aware UTC. Uses the start time when
    given, else end-of-day; falls back to UTC when no/invalid tz."""
    t = cal.parse_hhmm(event_time) or time(23, 59)
    local = datetime.combine(event_date, t)
    return cal.to_utc(local, tz) or local.replace(tzinfo=UTC)


def _snap_to_send_hour(raw_utc: datetime, tz: str | None, hour: int) -> datetime:
    """Move an instant to ``hour``:00 on the local date it falls on, back to UTC."""
    if tz:
        try:
            local = raw_utc.astimezone(ZoneInfo(tz))
            fired = local.replace(hour=hour, minute=0, second=0, microsecond=0)
            return fired.astimezone(UTC)
        except Exception:
            pass
    return raw_utc.replace(hour=hour, minute=0, second=0, microsecond=0)


def compute_slots(
    *,
    sent_at: datetime,
    event_date: Date,
    event_time: str | None,
    tz: str | None,
    cfg: ReminderConfig,
    now: datetime,
) -> list[Slot]:
    """Reminder slots for one recipient, in chronological order.

    For each configured offset: compute the raw instant, snap it to send_hour in the
    sender's tz, then drop it if it's in the past, at/before the send, or at/after the
    event. Remaining slots closer together than min_gap_hours are merged (keep the
    earlier), then the list is capped to max_per_recipient.
    """
    deadline = event_moment(event_date, event_time, tz)
    raw: list[tuple[datetime, str]] = []
    for label in cfg.offsets:
        if label == "halfway":
            instant = sent_at + (deadline - sent_at) / 2
        elif (m := _ND_RE.match(label)):
            instant = deadline - timedelta(days=int(m.group(1)))
        else:
            continue  # unknown offset label — ignore
        raw.append((_snap_to_send_hour(instant, tz, cfg.send_hour_local), label))

    kept: list[Slot] = []
    for fire, label in sorted(raw, key=lambda x: x[0]):
        if fire <= now or fire <= sent_at or fire >= deadline:
            continue
        if kept and (fire - kept[-1].scheduled_for) < timedelta(hours=cfg.min_gap_hours):
            continue  # too close to the previous slot — merge (keep the earlier)
        kept.append(Slot(scheduled_for=fire, label=label))
    return kept[: cfg.max_per_recipient]


def manual_slot(now: datetime) -> Slot:
    """An ad-hoc 'nudge now' slot (used for dateless events / manual sends)."""
    return Slot(scheduled_for=now, label="manual")


def still_needs_nudge(status: str, first_open_at: object, target: str) -> bool:
    """Whether a recipient is still eligible for a nudge under ``target``.

    Responded (coming/declined) always stops. 'no-rsvp' keeps nudging until they
    respond, even if they opened; 'not-clicked' stops as soon as they open."""
    if status in ("coming", "declined"):
        return False
    if target == "not-clicked":
        return status == "sent" and first_open_at is None
    return target == "no-rsvp"  # keep nudging anyone not yet coming/declined
