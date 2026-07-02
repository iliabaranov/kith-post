"""send_event orchestration: dry-run writes the outbox; self-only/live call Gmail
(mocked here) with the right recipient."""

import base64
import email

from sqlalchemy import select

from kith.config import SendMode, Settings
from kith.core.tracking import new_token
from kith.db.models import Event, Recipient, User
from kith.db.session import init_db, make_engine, make_session_factory
from kith.services import send


def _session(tmp_path):
    engine = make_engine(tmp_path / "s.sqlite3")
    init_db(engine)
    return make_session_factory(engine)()


def _seed(db, *, rt=None):
    u = User(google_sub="g", email="host@example.com", display_name="Mara", refresh_token=rt)
    db.add(u)
    db.commit()
    db.refresh(u)
    ev = Event(user_id=u.id, title="Party", message="Come", blocks={"rsvp": True})
    db.add(ev)
    db.commit()
    db.refresh(ev)
    db.add(Recipient(event_id=ev.id, email="a@example.com", name="Sam", token=new_token()))
    db.commit()
    return u, ev


def _to_of(raw_b64):
    return email.message_from_bytes(base64.urlsafe_b64decode(raw_b64))["To"]


def test_dry_run_writes_outbox_and_marks_sent(tmp_path):
    db = _session(tmp_path)
    u, ev = _seed(db)
    s = Settings(send_mode=SendMode.dry_run, data_dir=tmp_path / "data", base_url="https://x.ts.net")
    res = send.send_event(db, ev, u, s)
    assert (res.sent, res.failed) == (1, 0)
    files = list((tmp_path / "data" / "outbox" / ev.id).glob("*.eml"))
    assert len(files) == 1
    raw = files[0].read_text()
    assert "Party" in raw and "/i/" in raw  # subject + view link token
    assert db.execute(select(Recipient)).scalar_one().status == "sent"


def test_self_only_sends_to_the_host(tmp_path, monkeypatch):
    db = _session(tmp_path)
    u, ev = _seed(db, rt="rt-token")
    captured = {}

    def fake(settings, refresh_token, raw_b64, thread_id=None):
        captured["to"] = _to_of(raw_b64)
        captured["rt"] = refresh_token
        return {"id": "m1", "threadId": "t1"}

    monkeypatch.setattr("kith.services.gmail.gmail_send", fake)
    s = Settings(send_mode=SendMode.self_only, google_client_id="c", google_client_secret="s",
                 data_dir=tmp_path / "data", base_url="https://x")
    res = send.send_event(db, ev, u, s)
    assert res.sent == 1
    assert "host@example.com" in captured["to"]  # self-only -> to the host, not the guest
    assert captured["rt"] == "rt-token"
    r = db.execute(select(Recipient)).scalar_one()
    assert r.status == "sent" and r.msg_id_hdr == "m1" and r.thread_id == "t1"


def test_live_sends_to_the_recipient(tmp_path, monkeypatch):
    db = _session(tmp_path)
    u, ev = _seed(db, rt="rt")
    captured = {}

    def fake(settings, refresh_token, raw_b64, thread_id=None):
        captured["to"] = _to_of(raw_b64)
        return {"id": "x", "threadId": "y"}

    monkeypatch.setattr("kith.services.gmail.gmail_send", fake)
    s = Settings(send_mode=SendMode.live, google_client_id="c", google_client_secret="s",
                 data_dir=tmp_path / "data", base_url="https://x")
    send.send_event(db, ev, u, s)
    assert "a@example.com" in captured["to"]


def test_send_stamps_anchor_message_id_and_drops_host_line(tmp_path):
    db = _session(tmp_path)
    u, ev = _seed(db)
    s = Settings(send_mode=SendMode.dry_run, data_dir=tmp_path / "data", base_url="https://x")
    send.send_event(db, ev, u, s)
    r = db.execute(select(Recipient)).scalar_one()
    assert r.rfc822_message_id and r.rfc822_message_id.startswith("<")  # anchor stored
    raw = next((tmp_path / "data" / "outbox" / ev.id).glob("*.eml")).read_text()
    assert "Message-ID:" in raw
    assert "sent you an invitation" not in raw  # the extra host line is gone


def test_failed_send_leaves_recipient_queued(tmp_path, monkeypatch):
    db = _session(tmp_path)
    u, ev = _seed(db, rt="rt")

    def boom(*a, **k):
        raise RuntimeError("gmail down")

    monkeypatch.setattr("kith.services.gmail.gmail_send", boom)
    s = Settings(send_mode=SendMode.live, google_client_id="c", google_client_secret="s",
                 data_dir=tmp_path / "data", base_url="https://x")
    res = send.send_event(db, ev, u, s)
    assert (res.sent, res.failed) == (0, 1)
    assert db.execute(select(Recipient)).scalar_one().status == "queued"  # retryable
