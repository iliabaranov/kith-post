"""P5: the event-page reminders card + per-event toggle endpoint."""

import sqlite3

from kith.config import get_settings


def _db():
    return sqlite3.connect(get_settings().db_path)


def _event_id():
    return _db().execute("SELECT id FROM events LIMIT 1").fetchone()[0]


def _counts():
    return dict(_db().execute("SELECT status, COUNT(*) FROM reminders GROUP BY status").fetchall())


def _make(client, recipients="guest@example.com", **extra):
    client.post("/auth/dev-login")
    data = {"title": "Party", "recipients": recipients, "block_rsvp": "on"}
    data.update(extra)
    client.post("/events", data=data, follow_redirects=True)
    return _event_id()


def test_reminders_card_shown_for_dated_rsvp_event(client):
    eid = _make(client, block_date="on", event_date="2030-01-01")
    r = client.get(f"/events/{eid}")
    assert "Automatic reminders" in r.text
    assert "Turn off" in r.text  # on by default


def test_reminders_unavailable_without_date(client):
    eid = _make(client)  # rsvp but no date
    r = client.get(f"/events/{eid}")
    assert "Automatic reminders" not in r.text
    assert "Reminders need an event date" in r.text


def test_toggle_off_cancels_pending(client):
    eid = _make(client, block_date="on", event_date="2030-01-01")
    client.post(f"/events/{eid}/send")           # schedules pending reminders
    assert _counts().get("pending", 0) > 0

    client.post(f"/events/{eid}/reminders")       # no "enabled" field -> turn off
    assert _counts().get("pending", 0) == 0
    assert _counts().get("canceled", 0) > 0
    assert "Turn on" in client.get(f"/events/{eid}").text


def test_toggle_back_on_reschedules(client):
    eid = _make(client, block_date="on", event_date="2030-01-01")
    client.post(f"/events/{eid}/send")
    client.post(f"/events/{eid}/reminders")                       # off
    assert _counts().get("pending", 0) == 0
    client.post(f"/events/{eid}/reminders", data={"enabled": "1"})  # on
    assert _counts().get("pending", 0) > 0


def test_pre_send_explains_schedule(client):
    eid = _make(client, block_date="on", event_date="2030-01-01")
    page = client.get(f"/events/{eid}").text
    assert "Once you send" in page
    assert "1 week before" in page and "3 days before" in page


def test_after_send_lists_all_planned(client):
    eid = _make(client, block_date="on", event_date="2030-01-01")
    client.post(f"/events/{eid}/send")
    page = client.get(f"/events/{eid}").text
    assert "Planned:" in page
    assert 'class="reminders-list"' in page
    assert "Once you send" not in page  # already sent


def test_no_revert_after_all_replied(client):
    # Regression: with the only recipient replied (reminders canceled), the card
    # must NOT fall back to "Once you send" — the event has already gone out.
    eid = _make(client, block_date="on", event_date="2030-01-01")
    client.post(f"/events/{eid}/send")
    tok = _db().execute("SELECT token FROM recipients LIMIT 1").fetchone()[0]
    client.post(f"/i/{tok}/rsvp", data={"response": "coming"})
    page = client.get(f"/events/{eid}").text
    assert "Once you send" not in page
    assert "Nothing pending" in page


def test_each_planned_time_is_listed_once_however_many_guests(client):
    """The bug: a reminder row is per recipient while the schedule is per event,
    so the card listed the same three timestamps once per waiting guest — six
    guests turned three planned nudges into eighteen identical lines."""
    import re

    eid = _make(
        client,
        recipients="a@example.com, b@example.com, c@example.com, "
                   "d@example.com, e@example.com, f@example.com",
        block_date="on", event_date="2030-01-01",
    )
    client.post(f"/events/{eid}/send")
    page = client.get(f"/events/{eid}").text

    listed = re.search(r'<ul class="reminders-list">(.*?)</ul>', page, re.S).group(1)
    items = re.findall(r"<li>(.*?)</li>", listed, re.S)
    times = [re.sub(r"<[^>]+>", "", i).split("·")[0].strip() for i in items]
    assert len(times) == len(set(times)), f"duplicate times listed: {times}"
    # Three offsets are configured, and six guests must not multiply them.
    assert len(times) <= 3, f"expected at most 3 planned times, got {times}"

    # Instead of repeating, a time says how many people it covers.
    assert "to 6 people" in page
    assert "Nudging 6 people who haven" in page


def test_a_single_guest_reads_naturally(client):
    eid = _make(client, block_date="on", event_date="2030-01-01")
    client.post(f"/events/{eid}/send")
    page = client.get(f"/events/{eid}").text
    assert "Nudging 1 person who haven" in page
    assert "to 1 people" not in page      # no count for a single recipient


def test_a_distant_reminder_names_its_year(client):
    """The halfway slot for an event years out lands in a different year, and
    without the year the list reads as though it were out of order."""
    eid = _make(client, block_date="on", event_date="2030-01-01")
    client.post(f"/events/{eid}/send")
    page = client.get(f"/events/{eid}").text
    assert "2029" in page or "2028" in page or "2027" in page


def test_a_reminder_this_year_stays_terse(client):
    from datetime import date, timedelta

    soon = (date.today() + timedelta(days=20)).isoformat()
    eid = _make(client, block_date="on", event_date=soon)
    client.post(f"/events/{eid}/send")
    page = client.get(f"/events/{eid}").text
    import re

    listed = re.search(r'<ul class="reminders-list">(.*?)</ul>', page, re.S)
    if listed:  # a near event may have no slots left, which is fine
        assert str(date.today().year) not in listed.group(1)
