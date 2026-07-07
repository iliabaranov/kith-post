"""Per-card frame/background style (washi/clean/corners/postmark/matte)."""

import sqlite3

from kith.config import get_settings


def _db():
    return sqlite3.connect(get_settings().db_path)


def _event_id():
    return _db().execute("SELECT id FROM events LIMIT 1").fetchone()[0]


def _style_of(eid):
    return _db().execute("SELECT card_style FROM events WHERE id=?", (eid,)).fetchone()[0]


def test_create_persists_chosen_style(client):
    client.post("/auth/dev-login")
    client.post("/events", data={"title": "Dinner", "card_style": "matte",
                                 "recipients": "a@example.com", "block_rsvp": "on"})
    assert _style_of(_event_id()) == "matte"


def test_invalid_style_falls_back_to_washi(client):
    client.post("/auth/dev-login")
    client.post("/events", data={"title": "X", "card_style": "bogus",
                                 "recipients": "a@example.com"})
    assert _style_of(_event_id()) == "washi"


def test_default_style_is_washi(client):
    client.post("/auth/dev-login")
    client.post("/events", data={"title": "X", "recipients": "a@example.com"})
    assert _style_of(_event_id()) == "washi"


def test_edit_changes_style(client):
    client.post("/auth/dev-login")
    client.post("/events", data={"title": "X", "card_style": "washi",
                                 "recipients": "a@example.com"})
    eid = _event_id()
    client.post(f"/events/{eid}", data={"title": "X", "card_style": "postmark",
                                        "recipients": "a@example.com"})
    assert _style_of(eid) == "postmark"


def test_new_form_shows_the_picker(client):
    client.post("/auth/dev-login")
    f = client.get("/events/new").text
    assert 'name="card_style"' in f
    for v in ("washi", "clean", "corners", "postmark", "matte"):
        assert f'value="{v}"' in f


def test_preview_renders_the_frame_class(client):
    client.post("/auth/dev-login")
    client.post("/events", data={"title": "X", "card_style": "corners",
                                 "recipients": "a@example.com", "block_rsvp": "on"})
    p = client.get(f"/events/{_event_id()}/preview").text
    assert "card f-corners" in p


def test_invite_page_renders_the_frame_class(client):
    client.post("/auth/dev-login")
    client.post("/events", data={"title": "X", "card_style": "clean",
                                 "recipients": "a@example.com", "block_rsvp": "on"})
    token = _db().execute("SELECT token FROM recipients LIMIT 1").fetchone()[0]
    page = client.get(f"/i/{token}").text
    assert "card f-clean" in page
