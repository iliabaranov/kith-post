"""Optional Cc riders on a card send (cards without RSVP only)."""

import base64
import email
import json
import sqlite3

from kith.config import SendMode, Settings, get_settings
from kith.core.tracking import new_token
from kith.db.models import Event, Recipient, User
from kith.db.session import init_db, make_engine, make_session_factory
from kith.services import send

# ---- send-level (isolated ORM harness) ----

def _session(tmp_path):
    engine = make_engine(tmp_path / "s.sqlite3")
    init_db(engine)
    return make_session_factory(engine)()


def _seed_cc(db, cc, *, rsvp=False):
    u = User(google_sub="g", email="host@example.com", display_name="Mara", refresh_token="rt")
    db.add(u)
    db.commit()
    db.refresh(u)
    ev = Event(user_id=u.id, title="Bday", message="Hi", blocks={"rsvp": rsvp}, cc=cc)
    db.add(ev)
    db.commit()
    db.refresh(ev)
    db.add(Recipient(event_id=ev.id, email="grandma@example.com", name="Gran", token=new_token()))
    db.commit()
    return u, ev


def _outbox_msg(tmp_path, ev):
    raw = next((tmp_path / "data" / "outbox" / ev.id).glob("*.eml")).read_text()
    return email.message_from_string(raw)


def test_cc_header_present_in_dry_run(tmp_path):
    db = _session(tmp_path)
    cc = json.dumps([{"name": "Mom", "email": "mom@example.com"},
                     {"name": None, "email": "dad@example.com"}])
    u, ev = _seed_cc(db, cc)
    s = Settings(send_mode=SendMode.dry_run, data_dir=tmp_path / "data", base_url="https://x")
    send.send_event(db, ev, u, s)
    msg = _outbox_msg(tmp_path, ev)
    assert "grandma@example.com" in msg["To"]
    assert "mom@example.com" in msg["Cc"] and "dad@example.com" in msg["Cc"]


def test_cc_suppressed_on_rsvp_card(tmp_path):
    db = _session(tmp_path)
    u, ev = _seed_cc(db, json.dumps([{"email": "mom@example.com"}]), rsvp=True)
    s = Settings(send_mode=SendMode.dry_run, data_dir=tmp_path / "data", base_url="https://x")
    send.send_event(db, ev, u, s)
    assert not _outbox_msg(tmp_path, ev)["Cc"]


def test_cc_excluded_in_self_only(tmp_path, monkeypatch):
    db = _session(tmp_path)
    u, ev = _seed_cc(db, json.dumps([{"email": "mom@example.com"}]))
    captured = {}

    def fake_send(settings, rt, raw, thread_id=None):
        captured["raw"] = raw
        return {"id": "m", "threadId": "t"}

    monkeypatch.setattr("kith.services.gmail.gmail_send", fake_send)
    s = Settings(send_mode=SendMode.self_only, data_dir=tmp_path / "data", base_url="https://x",
                 google_client_id="c", google_client_secret="s")
    send.send_event(db, ev, u, s)
    msg = email.message_from_bytes(base64.urlsafe_b64decode(captured["raw"]))
    assert not msg["Cc"]                     # self-test must not email the family


# ---- routes + UI (dev-login session) ----

def _db():
    return sqlite3.connect(get_settings().db_path)


def _eid():
    return _db().execute("SELECT id FROM events LIMIT 1").fetchone()[0]


def _cc_raw(eid):
    return _db().execute("SELECT cc FROM events WHERE id=?", (eid,)).fetchone()[0]


def test_cc_saved_encrypted_and_prefilled(client):
    client.post("/auth/dev-login")
    client.post("/events", data={"title": "Bday", "recipients": "grandma@example.com",
                                 "cc": "Mom <mom@example.com>"})
    eid = _eid()
    assert _cc_raw(eid) and "mom@example.com" not in _cc_raw(eid)   # encrypted at rest
    assert "mom@example.com" in client.get(f"/events/{eid}/edit").text  # decrypted + prefilled


def test_cc_not_stored_on_rsvp_card(client):
    client.post("/auth/dev-login")
    client.post("/events", data={"title": "Party", "recipients": "a@example.com",
                                 "cc": "mom@example.com", "block_rsvp": "on"})
    assert _cc_raw(_eid()) is None


def test_adding_rsvp_clears_existing_cc(client):
    client.post("/auth/dev-login")
    client.post("/events", data={"title": "Bday", "recipients": "a@example.com",
                                 "cc": "mom@example.com"})
    eid = _eid()
    assert _cc_raw(eid) is not None
    client.post(f"/events/{eid}", data={"title": "Bday", "recipients": "a@example.com",
                                        "cc": "mom@example.com", "block_rsvp": "on"})
    assert _cc_raw(eid) is None


def test_event_page_shows_cc(client):
    client.post("/auth/dev-login")
    client.post("/events", data={"title": "Bday", "recipients": "a@example.com",
                                 "cc": "Mom <mom@example.com>"})
    page = client.get(f"/events/{_eid()}").text
    assert "CC'd on the email" in page and "Mom" in page


def test_form_has_cc_disclosure(client):
    client.post("/auth/dev-login")
    f = client.get("/events/new").text
    assert 'name="cc"' in f and "CC others" in f
