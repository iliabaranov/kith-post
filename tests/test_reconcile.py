"""P1: editing an event reconciles its recipient list in place, instead of the
old delete-and-recreate that wiped tokens, sent-state, and RSVPs on every save."""

import io
import sqlite3

from PIL import Image

from kith.config import get_settings


def _png_file():
    b = io.BytesIO()
    Image.new("RGB", (60, 40), "orange").save(b, "PNG")
    return {"image": ("card.png", b.getvalue(), "image/png")}


def _db():
    return sqlite3.connect(get_settings().db_path)


def _event_id():
    return _db().execute("SELECT id FROM events LIMIT 1").fetchone()[0]


def _rows():
    """(token, status) for every recipient — emails are encrypted, so we reason
    about identity via the stable token."""
    return _db().execute("SELECT token, status FROM recipients").fetchall()


def _form(recipients: str) -> dict:
    return {
        "title": "Party", "recipients": recipients,
        "block_rsvp": "on", "block_date": "on", "event_date": "2030-01-01",
    }


def _create(client, recipients: str):
    return client.post("/events", data=_form(recipients), files=_png_file(), follow_redirects=True)


def _edit(client, event_id: str, recipients: str):
    return client.post(f"/events/{event_id}", data=_form(recipients), follow_redirects=True)


def test_edit_keeps_matched_and_removes_absent(client):
    client.post("/auth/dev-login")
    _create(client, "a@example.com\nb@example.com")
    eid = _event_id()
    before = {t for t, _ in _rows()}
    assert len(before) == 2

    _edit(client, eid, "a@example.com\nc@example.com")
    after = {t for t, _ in _rows()}
    assert len(after) == 2
    # a@ kept its token; b removed; c added with a fresh token.
    assert len(before & after) == 1
    assert len(after - before) == 1


def test_edit_preserves_rsvp_on_kept_recipient(client):
    client.post("/auth/dev-login")
    _create(client, "a@example.com")
    eid = _event_id()
    (tok, status), = _rows()
    assert status == "queued"

    client.post(f"/i/{tok}/rsvp", data={"response": "coming", "party_size": "2"})
    assert _rows()[0][1] == "coming"

    _edit(client, eid, "a@example.com")  # keep the same person
    rows = _rows()
    assert len(rows) == 1
    assert rows[0][0] == tok          # same row (token unchanged)
    assert rows[0][1] == "coming"     # RSVP survived the edit


def test_edit_case_variant_matches_existing(client):
    client.post("/auth/dev-login")
    _create(client, "a@example.com")
    eid = _event_id()
    (tok, _), = _rows()

    _edit(client, eid, "A@Example.COM")   # same address, different case
    rows = _rows()
    assert len(rows) == 1
    assert rows[0][0] == tok              # matched, not re-added
