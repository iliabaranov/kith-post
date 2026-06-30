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
