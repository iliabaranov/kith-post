"""P3: the reminder sweep fires due reminders, re-checks eligibility, threads, and
never double-sends. Exercised via sweep_tick with an injected clock, in dry-run."""

import base64
import email
from datetime import UTC, date, datetime

from sqlalchemy import select

from kith.config import SendMode, Settings
from kith.core.tracking import new_token
from kith.db.models import Event, Recipient, Reminder, User
from kith.db.session import init_db, make_engine, make_session_factory
from kith.services import scheduler

PAST = datetime(2026, 6, 1, 12, tzinfo=UTC)
NOW = datetime(2026, 6, 2, 12, tzinfo=UTC)


def _factory(tmp_path):
    engine = make_engine(tmp_path / "s.sqlite3")
    init_db(engine)
    return make_session_factory(engine)


def _seed(db, *, status="sent", first_open=None, event_date=date(2999, 1, 1), thread="t1"):
    u = User(google_sub="g", email="host@example.com", display_name="Mara", refresh_token="rt")
    db.add(u)
    db.commit()
    db.refresh(u)
    ev = Event(user_id=u.id, title="Party", blocks={"rsvp": True}, event_date=event_date)
    db.add(ev)
    db.commit()
    db.refresh(ev)
    r = Recipient(
        event_id=ev.id, email="a@example.com", name="Sam", token=new_token(),
        status=status, first_open_at=first_open, thread_id=thread,
        sent_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return u, ev, r


def _reminder(db, ev, r, when, *, status="pending", label="7d"):
    rem = Reminder(event_id=ev.id, recipient_id=r.id, scheduled_for=when,
                   offset_label=label, status=status)
    db.add(rem)
    db.commit()
    db.refresh(rem)
    return rem


def _settings(tmp_path, mode=SendMode.dry_run):
    return Settings(send_mode=mode, data_dir=tmp_path / "data", base_url="https://x",
                    google_client_id="c", google_client_secret="s")


def test_due_reminder_sends_in_dry_run(tmp_path):
    f = _factory(tmp_path)
    db = f()
    _, ev, r = _seed(db)
    rem = _reminder(db, ev, r, PAST)
    db.close()

    res = scheduler.sweep_tick(f, _settings(tmp_path), now=NOW)
    assert (res.considered, res.sent) == (1, 1)
    assert f().get(Reminder, rem.id).status == "sent"
    files = list((tmp_path / "data" / "outbox" / ev.id / "reminders").glob("*.eml"))
    assert len(files) == 1
    assert "Re:" in files[0].read_text()


def test_future_reminder_not_fired(tmp_path):
    f = _factory(tmp_path)
    db = f()
    _, ev, r = _seed(db)
    rem = _reminder(db, ev, r, datetime(2027, 1, 1, tzinfo=UTC))
    db.close()

    res = scheduler.sweep_tick(f, _settings(tmp_path), now=NOW)
    assert res.considered == 0
    assert f().get(Reminder, rem.id).status == "pending"


def test_responded_recipient_skipped(tmp_path):
    f = _factory(tmp_path)
    db = f()
    _, ev, r = _seed(db, status="coming")
    rem = _reminder(db, ev, r, PAST)
    db.close()

    res = scheduler.sweep_tick(f, _settings(tmp_path), now=NOW)
    assert (res.sent, res.skipped) == (0, 1)
    got = f().get(Reminder, rem.id)
    assert got.status == "skipped"
    assert got.skip_reason == "engaged"


def test_opened_still_nudged_under_no_rsvp(tmp_path):
    # Default target is no-rsvp: opening the card does NOT stop the nudges.
    f = _factory(tmp_path)
    db = f()
    _, ev, r = _seed(db, first_open=datetime(2026, 5, 1, tzinfo=UTC))
    _reminder(db, ev, r, PAST)
    db.close()

    assert scheduler.sweep_tick(f, _settings(tmp_path), now=NOW).sent == 1


def test_past_event_skipped(tmp_path):
    f = _factory(tmp_path)
    db = f()
    _, ev, r = _seed(db, event_date=date(2000, 1, 1))
    rem = _reminder(db, ev, r, PAST)
    db.close()

    scheduler.sweep_tick(f, _settings(tmp_path), now=NOW)
    got = f().get(Reminder, rem.id)
    assert got.status == "skipped"
    assert got.skip_reason == "after_event"


def test_cap_reached_skips(tmp_path):
    f = _factory(tmp_path)
    db = f()
    _, ev, r = _seed(db)
    for _ in range(3):
        _reminder(db, ev, r, PAST, status="sent")
    due = _reminder(db, ev, r, PAST)
    db.close()

    scheduler.sweep_tick(f, _settings(tmp_path), now=NOW)
    got = f().get(Reminder, due.id)
    assert got.status == "skipped"
    assert got.skip_reason == "capped"


def test_self_only_threads_to_host(tmp_path, monkeypatch):
    f = _factory(tmp_path)
    db = f()
    _, ev, r = _seed(db, thread="TID")
    _reminder(db, ev, r, PAST)
    db.close()
    cap = {}

    def fake(settings, refresh_token, raw_b64, thread_id=None):
        cap["to"] = email.message_from_bytes(base64.urlsafe_b64decode(raw_b64))["To"]
        cap["thread"] = thread_id
        return {"id": "m", "threadId": "TID"}

    monkeypatch.setattr("kith.services.gmail.gmail_send", fake)
    res = scheduler.sweep_tick(f, _settings(tmp_path, SendMode.self_only), now=NOW)
    assert res.sent == 1
    assert "host@example.com" in cap["to"]
    assert cap["thread"] == "TID"


def test_idempotent_across_ticks(tmp_path):
    f = _factory(tmp_path)
    db = f()
    _, ev, r = _seed(db)
    _reminder(db, ev, r, PAST)
    db.close()

    s = _settings(tmp_path)
    assert scheduler.sweep_tick(f, s, now=NOW).sent == 1
    assert scheduler.sweep_tick(f, s, now=NOW).sent == 0  # already sent


# --- P4: schedule creation + cancellation ---

SCHED_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _pending(db):
    return db.execute(select(Reminder).where(Reminder.status == "pending")).scalars().all()


def test_schedule_creates_pending_for_sent(tmp_path):
    f = _factory(tmp_path)
    db = f()
    _, ev, r = _seed(db)
    n = scheduler.schedule_event_reminders(db, ev, _settings(tmp_path), now=SCHED_NOW)
    assert n >= 1
    assert len(_pending(db)) == n


def test_no_reminders_without_rsvp_block(tmp_path):
    f = _factory(tmp_path)
    db = f()
    _, ev, r = _seed(db)
    ev.blocks = {}
    db.commit()
    assert scheduler.schedule_event_reminders(db, ev, _settings(tmp_path), now=SCHED_NOW) == 0


def test_no_reminders_without_date(tmp_path):
    f = _factory(tmp_path)
    db = f()
    _, ev, r = _seed(db, event_date=None)
    assert scheduler.schedule_event_reminders(db, ev, _settings(tmp_path), now=SCHED_NOW) == 0


def test_reschedule_is_idempotent(tmp_path):
    f = _factory(tmp_path)
    db = f()
    _, ev, r = _seed(db)
    s = _settings(tmp_path)
    n1 = scheduler.schedule_event_reminders(db, ev, s, now=SCHED_NOW)
    n2 = scheduler.schedule_event_reminders(db, ev, s, now=SCHED_NOW)
    assert n1 == n2
    assert len(_pending(db)) == n2  # rebuilt, not duplicated


def test_cancel_pending_reminders(tmp_path):
    f = _factory(tmp_path)
    db = f()
    _, ev, r = _seed(db)
    scheduler.schedule_event_reminders(db, ev, _settings(tmp_path), now=SCHED_NOW)
    scheduler.cancel_pending_reminders(db, r.id)
    assert _pending(db) == []
    canceled = db.execute(select(Reminder).where(Reminder.status == "canceled")).scalars().all()
    assert len(canceled) >= 1
