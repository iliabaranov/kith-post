"""G7-P1: reply note (always) + allergies (per-event toggle) on the RSVP."""

import json
import sqlite3

from kith.config import get_settings


def _db():
    return sqlite3.connect(get_settings().db_path)


def _eid():
    return _db().execute("SELECT id FROM events LIMIT 1").fetchone()[0]


def _token():
    return _db().execute("SELECT token FROM recipients LIMIT 1").fetchone()[0]


def _blocks():
    return json.loads(_db().execute("SELECT blocks FROM events LIMIT 1").fetchone()[0])


def _make(client, **extra):
    client.post("/auth/dev-login")
    data = {"title": "Party", "recipients": "g@example.com", "block_rsvp": "on"}
    data.update(extra)
    client.post("/events", data=data, follow_redirects=True)
    return _eid()


def test_allergies_block_persists(client):
    _make(client, block_allergies="on")
    assert _blocks()["allergies"] is True


def test_note_always_shown_allergies_only_when_enabled(client):
    _make(client, block_allergies="on")
    page = client.get(f"/i/{_token()}").text
    assert 'name="note"' in page
    assert 'name="allergies"' in page


def test_allergies_field_hidden_when_block_off(client):
    _make(client)  # no allergies block
    page = client.get(f"/i/{_token()}").text
    assert 'name="note"' in page          # note is always available
    assert 'name="allergies"' not in page  # allergies only when enabled


def test_rsvp_saves_note_and_allergies(client):
    eid = _make(client, block_allergies="on")
    client.post(f"/i/{_token()}/rsvp", data={
        "response": "coming", "party_size": "1",
        "note": "Cannot wait!", "allergies": "peanuts",
    })
    page = client.get(f"/events/{eid}").text
    assert "Cannot wait!" in page
    assert "peanuts" in page
    assert "Allergies:" in page and "Note:" in page


def test_allergies_dropped_when_block_off(client):
    eid = _make(client)  # no allergies block
    client.post(f"/i/{_token()}/rsvp", data={
        "response": "coming", "party_size": "1",
        "note": "see you", "allergies": "shellfish",
    })
    page = client.get(f"/events/{eid}").text
    assert "see you" in page          # note still saved
    assert "shellfish" not in page    # allergies ignored when not asked
