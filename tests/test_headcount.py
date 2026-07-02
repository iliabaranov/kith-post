"""G7-P2: adults + kids headcount steppers."""

import sqlite3

from kith.config import get_settings


def _db():
    return sqlite3.connect(get_settings().db_path)


def _eid():
    return _db().execute("SELECT id FROM events LIMIT 1").fetchone()[0]


def _token():
    return _db().execute("SELECT token FROM recipients LIMIT 1").fetchone()[0]


def _recip():
    return _db().execute(
        "SELECT status, party_size, adults, kids FROM recipients LIMIT 1"
    ).fetchone()


def _make(client, headcount=True, **extra):
    client.post("/auth/dev-login")
    data = {"title": "Party", "recipients": "g@example.com", "block_rsvp": "on"}
    if headcount:
        data["block_headcount"] = "on"
    data.update(extra)
    client.post("/events", data=data, follow_redirects=True)
    return _eid()


def test_adults_and_kids_saved(client):
    _make(client)
    client.post(f"/i/{_token()}/rsvp", data={"response": "coming", "adults": "2", "kids": "3"})
    assert _recip() == ("coming", 5, 2, 3)


def test_cap_applies_to_total(client):
    _make(client, headcount_max="3")
    client.post(f"/i/{_token()}/rsvp", data={"response": "coming", "adults": "2", "kids": "5"})
    status, ps, a, k = _recip()
    assert (a, k, ps) == (2, 1, 3)  # kids trimmed so adults+kids == cap


def test_at_least_one_adult(client):
    _make(client)
    client.post(f"/i/{_token()}/rsvp", data={"response": "coming", "adults": "0", "kids": "2"})
    _, ps, a, k = _recip()
    assert a == 1 and k == 2 and ps == 3


def test_no_headcount_block_is_single_guest(client):
    _make(client, headcount=False)
    client.post(f"/i/{_token()}/rsvp", data={"response": "coming", "adults": "4", "kids": "2"})
    status, ps, a, k = _recip()
    assert status == "coming" and ps == 1 and a is None and k is None


def test_two_steppers_rendered(client):
    _make(client)
    page = client.get(f"/i/{_token()}").text
    assert 'id="countAdults"' in page
    assert 'id="countKids"' in page


def test_event_page_shows_split(client):
    eid = _make(client)
    client.post(f"/i/{_token()}/rsvp", data={"response": "coming", "adults": "2", "kids": "3"})
    page = client.get(f"/events/{eid}").text
    assert "2 adults" in page
    assert "3 kids" in page
