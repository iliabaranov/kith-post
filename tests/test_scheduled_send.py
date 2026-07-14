"""Scheduling a card's send for a future time (fired by the sweep worker)."""

import sqlite3
from datetime import UTC, datetime

from kith.config import SendMode, Settings, get_settings
from kith.core.tracking import new_token
from kith.db.models import Event, Recipient, User
from kith.db.session import init_db, make_engine, make_session_factory
from kith.services import scheduler

PAST = datetime(2026, 6, 1, 12, tzinfo=UTC)
NOW = datetime(2026, 6, 2, 12, tzinfo=UTC)


# ---- sweep firing (isolated ORM harness, dry-run) ----

def _factory(tmp_path):
    engine = make_engine(tmp_path / "s.sqlite3")
    init_db(engine)
    return make_session_factory(engine)


def _settings(tmp_path, mode=SendMode.dry_run):
    return Settings(send_mode=mode, data_dir=tmp_path / "data", base_url="https://x",
                    google_client_id="c", google_client_secret="s")


def _seed(db, *, when, status="queued", token_rt="rt"):
    u = User(google_sub="g", email="host@example.com", display_name="Mara", refresh_token=token_rt)
    db.add(u)
    db.commit()
    db.refresh(u)
    ev = Event(user_id=u.id, title="Card", blocks={"rsvp": True}, scheduled_send_at=when)
    db.add(ev)
    db.commit()
    db.refresh(ev)
    r = Recipient(event_id=ev.id, email="a@example.com", name="Sam",
                  token=new_token(), status=status)
    db.add(r)
    db.commit()
    db.refresh(r)
    return u.id, ev.id, r.id


def test_due_scheduled_send_fires_and_clears(tmp_path):
    f = _factory(tmp_path)
    db = f()
    _, eid, rid = _seed(db, when=PAST)
    db.close()

    res = scheduler.sweep_tick(f, _settings(tmp_path), now=NOW)
    assert res.scheduled == 1
    db2 = f()
    assert db2.get(Recipient, rid).status == "sent"
    assert db2.get(Event, eid).scheduled_send_at is None
    files = list((tmp_path / "data" / "outbox" / eid).glob("*.eml"))
    assert len(files) == 1


def test_future_scheduled_send_not_fired(tmp_path):
    f = _factory(tmp_path)
    db = f()
    _, eid, rid = _seed(db, when=datetime(2099, 1, 1, tzinfo=UTC))
    db.close()

    res = scheduler.sweep_tick(f, _settings(tmp_path), now=NOW)
    assert res.scheduled == 0
    db2 = f()
    assert db2.get(Recipient, rid).status == "queued"
    assert db2.get(Event, eid).scheduled_send_at is not None


def test_scheduled_send_kept_when_no_token_in_live(tmp_path):
    f = _factory(tmp_path)
    db = f()
    _, eid, rid = _seed(db, when=PAST, token_rt=None)
    db.close()

    res = scheduler.sweep_tick(f, _settings(tmp_path, mode=SendMode.live), now=NOW)
    assert res.scheduled == 0                       # can't send — no network attempted
    db2 = f()
    assert db2.get(Recipient, rid).status == "queued"
    assert db2.get(Event, eid).scheduled_send_at is not None  # schedule retained


# ---- routes + UI (dev-login session) ----

def _db():
    return sqlite3.connect(get_settings().db_path)


def _event_id():
    return _db().execute("SELECT id FROM events LIMIT 1").fetchone()[0]


def _sched(eid):
    return _db().execute("SELECT scheduled_send_at FROM events WHERE id=?", (eid,)).fetchone()[0]


def _make_card(client, recipients="a@example.com"):
    client.post("/auth/dev-login")
    client.post("/events", data={"title": "Card", "recipients": recipients, "block_rsvp": "on"})
    return _event_id()


def test_schedule_persists_a_future_time(client):
    eid = _make_card(client)
    r = client.post(f"/events/{eid}/schedule",
                    data={"send_date": "2099-12-31", "send_time": "09:00",
                          "timezone": "America/Toronto"}, follow_redirects=False)
    assert r.status_code == 303 and "scheduled=1" in r.headers["location"]
    assert _sched(eid) is not None


def test_schedule_rejects_a_past_time(client):
    eid = _make_card(client)
    r = client.post(f"/events/{eid}/schedule",
                    data={"send_date": "2020-01-01", "send_time": "09:00", "timezone": "UTC"},
                    follow_redirects=False)
    assert "schedule_error=1" in r.headers["location"]
    assert _sched(eid) is None


def test_schedule_requires_someone_to_send_to(client):
    eid = _make_card(client, recipients="")   # no recipients queued
    r = client.post(f"/events/{eid}/schedule",
                    data={"send_date": "2099-12-31", "send_time": "09:00", "timezone": "UTC"},
                    follow_redirects=False)
    assert "schedule_error=1" in r.headers["location"]
    assert _sched(eid) is None


def test_unschedule_clears_it(client):
    eid = _make_card(client)
    client.post(f"/events/{eid}/schedule",
                data={"send_date": "2099-12-31", "send_time": "09:00", "timezone": "UTC"})
    assert _sched(eid) is not None
    client.post(f"/events/{eid}/unschedule")
    assert _sched(eid) is None


def test_send_now_clears_any_schedule(client):
    eid = _make_card(client)
    client.post(f"/events/{eid}/schedule",
                data={"send_date": "2099-12-31", "send_time": "09:00", "timezone": "UTC"})
    client.post(f"/events/{eid}/send")   # dry-run send
    assert _sched(eid) is None


def test_event_page_shows_disclosure_then_banner(client):
    eid = _make_card(client)
    assert "Schedule send for later" in client.get(f"/events/{eid}").text
    client.post(f"/events/{eid}/schedule",
                data={"send_date": "2099-12-31", "send_time": "09:00", "timezone": "UTC"})
    page = client.get(f"/events/{eid}").text
    assert "Scheduled to send" in page
    assert "Cancel schedule" in page


def test_dashboard_flags_scheduled_card(client):
    eid = _make_card(client)
    client.post(f"/events/{eid}/schedule",
                data={"send_date": "2099-12-31", "send_time": "09:00", "timezone": "UTC"})
    page = client.get("/").text
    assert 'event-flag">scheduled' in page
    assert "event-row is-scheduled" in page
    assert "Scheduled to send 12/31/99" in page   # shortform MM/DD/YY, in the card's tz
