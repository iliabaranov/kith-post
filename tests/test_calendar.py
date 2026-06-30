from datetime import date, datetime, time

from kith.core.calendar import CalEvent, build_google_url, build_ics, pretty_time

DTSTAMP = datetime(2026, 1, 1, 0, 0, 0)


def _ev(**kw) -> CalEvent:
    base = dict(
        title="Maya turns five!", date=date(2026, 5, 4), start=time(15, 0), end=time(17, 0),
        location="14 Linden St", details="Come play.", tz="America/Toronto", uid="e1@kith.post",
    )
    base.update(kw)
    return CalEvent(**base)


def test_pretty_time():
    assert pretty_time("15:00") == "3:00 pm"
    assert pretty_time("00:30") == "12:30 am"
    assert pretty_time("12:00") == "12:00 pm"
    assert pretty_time("9:05") == "9:05 am"
    assert pretty_time("afternoon") == "afternoon"  # passthrough for non-HH:MM
    assert pretty_time("") == ""


def test_google_url_timed_uses_utc_from_tz():
    url = build_google_url(_ev())
    assert url.startswith("https://calendar.google.com/calendar/render?")
    # 15:00 America/Toronto (EDT, -4) -> 19:00Z; 17:00 -> 21:00Z
    assert "dates=20260504T190000Z%2F20260504T210000Z" in url
    assert "Maya" in url
    assert "Linden" in url


def test_google_url_all_day_when_no_time():
    url = build_google_url(_ev(start=None, end=None))
    assert "dates=20260504%2F20260505" in url  # end is exclusive (next day)


def test_no_date_means_no_calendar():
    assert build_google_url(_ev(date=None)) is None
    assert build_ics(_ev(date=None), dtstamp=DTSTAMP) is None


def test_ics_timed():
    ics = build_ics(_ev(), dtstamp=DTSTAMP)
    assert "BEGIN:VCALENDAR" in ics and "BEGIN:VEVENT" in ics
    assert "DTSTART:20260504T190000Z" in ics  # converted to UTC
    assert "DTEND:20260504T210000Z" in ics
    assert "SUMMARY:Maya turns five!" in ics
    assert "UID:e1@kith.post" in ics
    assert ics.endswith("\r\n")


def test_ics_all_day():
    ics = build_ics(_ev(start=None, end=None), dtstamp=DTSTAMP)
    assert "DTSTART;VALUE=DATE:20260504" in ics
    assert "DTEND;VALUE=DATE:20260505" in ics
