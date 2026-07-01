"""Add-to-calendar links — pure, testable.

Builds a Google Calendar "template" URL and an .ics body from an event's
structured date/time/timezone. Times are stored as "HH:MM" (24h); a missing time
means an all-day event; a missing date means no calendar at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as Date
from datetime import time as Time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

DEFAULT_DURATION = timedelta(hours=2)
GOOGLE_BASE = "https://calendar.google.com/calendar/render"


def parse_hhmm(s: str | None) -> Time | None:
    if not s:
        return None
    try:
        h, m = s.strip().split(":")
        return Time(int(h), int(m))
    except (ValueError, TypeError):
        return None


def pretty_time(s: str | None) -> str:
    """'15:00' -> '3:00 pm'. Anything that isn't HH:MM passes through unchanged."""
    t = parse_hhmm(s)
    if t is None:
        return s or ""
    hour = t.hour % 12 or 12
    ampm = "am" if t.hour < 12 else "pm"
    return f"{hour}:{t.minute:02d} {ampm}"


@dataclass(frozen=True)
class CalEvent:
    title: str
    date: Date | None
    start: Time | None
    end: Time | None
    location: str | None
    details: str | None
    tz: str | None
    uid: str


def from_event(ev, rsvp_url: str | None = None) -> CalEvent:  # noqa: ANN001 — a DB Event
    """Build a CalEvent from a DB Event. When rsvp_url is given (the invitee's own
    invitation link), a "change your answer" line is appended under the message so
    the calendar entry itself carries a way back to the RSVP page."""
    blocks = ev.blocks or {}
    show_time = bool(blocks.get("time"))
    message = ev.message if blocks.get("message") else None
    details = message
    if rsvp_url:
        line = f"Need to change your answer?\n{rsvp_url}"
        details = f"{message}\n\n{line}" if message else line
    return CalEvent(
        title=ev.title or "Invitation",
        date=ev.event_date if blocks.get("date") else None,
        start=parse_hhmm(ev.event_time) if show_time else None,
        end=parse_hhmm(ev.event_end_time) if show_time else None,
        location=ev.location if blocks.get("location") else None,
        details=details,
        tz=ev.timezone,
        uid=f"{ev.id}@kith.post",
    )


def _span(ev: CalEvent) -> tuple[datetime, datetime, bool]:
    """(start_dt, end_dt, all_day) in naive local terms."""
    if ev.start is None:
        s = datetime.combine(ev.date, Time(0, 0))
        return s, s + timedelta(days=1), True
    s = datetime.combine(ev.date, ev.start)
    if ev.end is not None and ev.end > ev.start:
        e = datetime.combine(ev.date, ev.end)
    else:
        e = s + DEFAULT_DURATION
    return s, e, False


def _to_utc(dt: datetime, tz: str | None) -> datetime | None:
    if not tz:
        return None
    try:
        return dt.replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)
    except Exception:
        return None


def to_utc(dt: datetime, tz: str | None) -> datetime | None:
    """Public: naive local datetime + IANA tz → aware UTC (None if tz missing/invalid)."""
    return _to_utc(dt, tz)


def build_google_url(ev: CalEvent) -> str | None:
    if ev.date is None:
        return None
    s, e, all_day = _span(ev)
    params: dict[str, str] = {"action": "TEMPLATE", "text": ev.title or ""}
    if all_day:
        params["dates"] = f"{s:%Y%m%d}/{e:%Y%m%d}"  # end is exclusive
    else:
        su, eu = _to_utc(s, ev.tz), _to_utc(e, ev.tz)
        if su and eu:
            params["dates"] = f"{su:%Y%m%dT%H%M%SZ}/{eu:%Y%m%dT%H%M%SZ}"
        else:  # no/invalid tz -> floating local time
            params["dates"] = f"{s:%Y%m%dT%H%M%S}/{e:%Y%m%dT%H%M%S}"
    if ev.details:
        params["details"] = ev.details
    if ev.location:
        params["location"] = ev.location
    return f"{GOOGLE_BASE}?{urlencode(params)}"


def _esc(s: str | None) -> str:
    return (
        (s or "")
        .replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\n", "\\n")
    )


def build_ics(ev: CalEvent, *, dtstamp: datetime) -> str | None:
    if ev.date is None:
        return None
    s, e, all_day = _span(ev)
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Kith Post//EN", "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT", f"UID:{ev.uid}", f"DTSTAMP:{dtstamp:%Y%m%dT%H%M%SZ}",
    ]
    if all_day:
        lines.append(f"DTSTART;VALUE=DATE:{s:%Y%m%d}")
        lines.append(f"DTEND;VALUE=DATE:{e:%Y%m%d}")
    else:
        su, eu = _to_utc(s, ev.tz), _to_utc(e, ev.tz)
        if su and eu:
            lines.append(f"DTSTART:{su:%Y%m%dT%H%M%SZ}")
            lines.append(f"DTEND:{eu:%Y%m%dT%H%M%SZ}")
        else:
            lines.append(f"DTSTART:{s:%Y%m%dT%H%M%S}")
            lines.append(f"DTEND:{e:%Y%m%dT%H%M%S}")
    lines.append(f"SUMMARY:{_esc(ev.title)}")
    if ev.location:
        lines.append(f"LOCATION:{_esc(ev.location)}")
    if ev.details:
        lines.append(f"DESCRIPTION:{_esc(ev.details)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"
