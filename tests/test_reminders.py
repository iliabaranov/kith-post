"""P2: pure reminder slot computation (G5). Deterministic via injected `now`."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from kith.core.reminders import (
    ReminderConfig,
    compute_slots,
    resolve_reminder_config,
    still_needs_nudge,
)

TZ = "America/Toronto"
CFG = ReminderConfig()  # defaults: no-rsvp, halfway/7d/3d, 9am, 24h gap, cap 3


def _utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_three_slots_at_9am_local():
    # Sent 30 days out; evening event; expect 3 slots each at 09:00 America/Toronto.
    sent = _utc(2026, 6, 1, 12, 0)
    slots = compute_slots(
        sent_at=sent, event_date=date(2026, 7, 1), event_time="18:00", tz=TZ,
        cfg=CFG, now=_utc(2026, 6, 1, 12, 30),
    )
    assert [s.label for s in slots] == ["halfway", "7d", "3d"]
    for s in slots:
        local = s.scheduled_for.astimezone(ZoneInfo(TZ))
        assert (local.hour, local.minute) == (9, 0)
    # 7d before Jul 1 -> Jun 24 09:00 local; 3d -> Jun 28 09:00 local
    assert slots[1].scheduled_for.astimezone(ZoneInfo(TZ)).date() == date(2026, 6, 24)
    assert slots[2].scheduled_for.astimezone(ZoneInfo(TZ)).date() == date(2026, 6, 28)


def test_past_and_before_send_slots_dropped():
    # Event only 2 days out: 7d and 3d are already in the past, halfway is tiny.
    sent = _utc(2026, 6, 29, 12, 0)
    slots = compute_slots(
        sent_at=sent, event_date=date(2026, 7, 1), event_time="18:00", tz=TZ,
        cfg=CFG, now=_utc(2026, 6, 29, 12, 30),
    )
    for s in slots:
        assert s.scheduled_for > sent
        assert s.scheduled_for < datetime(2026, 7, 1, 22, tzinfo=UTC)  # before event
    assert len(slots) <= 1


def test_min_gap_merges_close_slots():
    # A 1-hour gap forces all computable slots to collapse to one.
    cfg = ReminderConfig(min_gap_hours=1000)
    sent = _utc(2026, 6, 1, 12, 0)
    slots = compute_slots(
        sent_at=sent, event_date=date(2026, 7, 1), event_time="18:00", tz=TZ,
        cfg=cfg, now=_utc(2026, 6, 1, 12, 30),
    )
    assert len(slots) == 1  # first kept, rest merged away


def test_cap_limits_count():
    cfg = ReminderConfig(max_per_recipient=2)
    slots = compute_slots(
        sent_at=_utc(2026, 6, 1), event_date=date(2026, 7, 1), event_time="18:00", tz=TZ,
        cfg=cfg, now=_utc(2026, 6, 1, 1),
    )
    assert len(slots) == 2


def test_no_tz_degrades_to_utc():
    slots = compute_slots(
        sent_at=_utc(2026, 6, 1), event_date=date(2026, 7, 1), event_time="18:00", tz=None,
        cfg=CFG, now=_utc(2026, 6, 1, 1),
    )
    assert slots and all(s.scheduled_for.hour == 9 for s in slots)  # snapped in UTC


def test_all_day_event_uses_end_of_day():
    slots = compute_slots(
        sent_at=_utc(2026, 6, 1), event_date=date(2026, 7, 1), event_time=None, tz=TZ,
        cfg=CFG, now=_utc(2026, 6, 1, 1),
    )
    assert slots  # deadline is end-of-day, slots still computed


def test_still_needs_nudge():
    # Responded stops under both targets.
    assert not still_needs_nudge("coming", None, "no-rsvp")
    assert not still_needs_nudge("declined", None, "not-clicked")
    # no-rsvp: keeps nudging even after opening.
    assert still_needs_nudge("sent", datetime.now(UTC), "no-rsvp")
    assert still_needs_nudge("sent", None, "no-rsvp")
    # not-clicked: opening stops it.
    assert still_needs_nudge("sent", None, "not-clicked")
    assert not still_needs_nudge("sent", datetime.now(UTC), "not-clicked")


def test_resolve_config_merges_override():
    base = ReminderConfig()
    merged = resolve_reminder_config(base, {"enabled": False, "max_per_recipient": 5})
    assert merged.enabled is False
    assert merged.max_per_recipient == 5
    assert merged.target == base.target  # untouched fields fall through
    assert resolve_reminder_config(base, None) == base
