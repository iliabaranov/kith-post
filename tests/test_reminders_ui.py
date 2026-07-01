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
