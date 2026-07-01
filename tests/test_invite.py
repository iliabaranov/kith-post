"""Public recipient pages reached by token (no auth)."""

import io
import sqlite3

from PIL import Image

from kith.config import get_settings


def _png():
    b = io.BytesIO()
    Image.new("RGB", (40, 30), "teal").save(b, "PNG")
    return b.getvalue()


def _token():
    return sqlite3.connect(get_settings().db_path).execute(
        "SELECT token FROM recipients LIMIT 1"
    ).fetchone()[0]


def _recip(field):
    return sqlite3.connect(get_settings().db_path).execute(
        f"SELECT {field} FROM recipients LIMIT 1"
    ).fetchone()[0]


def _make_event(client, **extra):
    """Create an event with one recipient and return that recipient's token."""
    client.post("/auth/dev-login")
    data = {"title": "Party", "recipients": "guest@example.com", "block_rsvp": "on"}
    data.update(extra)
    client.post("/events", data=data, follow_redirects=True)
    return _token()


def test_invite_landing_renders(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={"title": "Maya turns five!", "recipients": "a@example.com", "block_rsvp": "on"},
        files={"image": ("c.png", _png(), "image/png")},
        follow_redirects=True,
    )
    tok = _token()
    r = client.get(f"/i/{tok}")
    assert r.status_code == 200
    assert "Maya turns five!" in r.text
    assert "I'll be there" in r.text       # RSVP rendered
    assert f"/i/{tok}/image" in r.text     # token-scoped image
    assert "invite.js" in r.text


def test_invite_bad_token_is_404(client):
    assert client.get("/i/does-not-exist").status_code == 404


# ---- G4: RSVP persistence + "Opened" ----

def test_landing_visit_marks_opened(client):
    tok = _make_event(client)
    assert _recip("first_open_at") is None  # not opened until visited
    client.get(f"/i/{tok}")
    assert _recip("first_open_at") is not None


def test_rsvp_coming_persists_and_renders(client):
    tok = _make_event(client)
    r = client.post(f"/i/{tok}/rsvp", data={"response": "coming"}, follow_redirects=True)
    assert _recip("status") == "coming" and _recip("rsvp_at") is not None
    assert "see you there" in r.text.lower()
    assert 'class="stamp coming show"' in r.text  # stamp rendered from server state


def test_rsvp_declined_persists(client):
    tok = _make_event(client)
    client.post(f"/i/{tok}/rsvp", data={"response": "declined"}, follow_redirects=True)
    assert _recip("status") == "declined" and _recip("party_size") is None


def test_party_size_clamped_to_headcount_max(client):
    tok = _make_event(client, block_headcount="on", headcount_max="2")
    client.post(f"/i/{tok}/rsvp", data={"response": "coming", "party_size": "9"})
    assert _recip("party_size") == 2  # server clamps, doesn't trust the stepper


def test_change_response_reopens_the_choices(client):
    tok = _make_event(client)
    client.post(f"/i/{tok}/rsvp", data={"response": "coming"}, follow_redirects=True)
    page = client.get(f"/i/{tok}?edit=1").text
    assert "I'll be there" in page and 'class="stamp coming show"' not in page


def test_passed_event_locks_rsvp(client):
    tok = _make_event(client, block_date="on", event_date="2020-01-01")
    client.post(f"/i/{tok}/rsvp", data={"response": "coming"}, follow_redirects=True)
    assert _recip("status") != "coming"  # ignored — replies are closed
    assert "already passed" in client.get(f"/i/{tok}").text


def test_rsvp_bad_token_is_404(client):
    assert client.post("/i/nope/rsvp", data={"response": "coming"}).status_code == 404


def test_invite_image_served_by_token(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={"title": "x", "recipients": "a@example.com"},
        files={"image": ("c.png", _png(), "image/png")},
        follow_redirects=True,
    )
    img = client.get(f"/i/{_token()}/image")
    assert img.status_code == 200
    assert "image" in img.headers["content-type"]


def test_invite_ics_served_by_token(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={"title": "x", "recipients": "a@example.com",
              "block_date": "on", "event_date": "2026-05-04"},
        follow_redirects=True,
    )
    ics = client.get(f"/i/{_token()}/calendar.ics")
    assert ics.status_code == 200
    assert "calendar" in ics.headers["content-type"]
    assert "BEGIN:VCALENDAR" in ics.text
