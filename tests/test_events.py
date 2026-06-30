"""Compose-a-card flow (G2), via the dev-login session."""

import io
import sqlite3

from PIL import Image

from kith.config import get_settings


def _png_bytes(size=(60, 40), color="orange") -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


def _db():
    return sqlite3.connect(get_settings().db_path)


def test_new_event_form_requires_login(client):
    r = client.get("/events/new", follow_redirects=False)
    assert r.status_code == 303


def test_create_full_invite_and_preview(client):
    client.post("/auth/dev-login")
    r = client.post(
        "/events",
        data={
            "title": "Maya turns five!",
            "message": "Come play in the garden.",
            "event_date": "2026-05-04",
            "event_time": "3:00 pm",
            "location": "14 Linden St",
            "recipients": "a@example.com\nb@example.com",
            "block_message": "on", "block_date": "on", "block_time": "on",
            "block_location": "on", "block_rsvp": "on",
        },
        files={"image": ("card.png", _png_bytes(), "image/png")},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "Maya turns five!" in r.text
    assert "2 recipients" in r.text
    assert "/assets/" in r.text          # image is embedded in the preview
    assert "I'll be there" in r.text     # RSVP block rendered


def test_recipient_emails_encrypted_at_rest(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={"title": "x", "recipients": "secret@example.com"},
        follow_redirects=True,
    )
    raw = _db().execute("SELECT email FROM recipients LIMIT 1").fetchone()[0]
    assert "@" not in raw
    assert raw.startswith("gAAAA")      # Fernet token, not plaintext


def test_holiday_card_mode_has_no_rsvp(client):
    client.post("/auth/dev-login")
    r = client.post(
        "/events",
        data={"title": "Winter cheer", "message": "Happy holidays!",
              "block_message": "on", "recipients": ""},
        follow_redirects=True,
    )
    assert "Winter cheer" in r.text
    assert "I'll be there" not in r.text  # RSVP off -> plain card (literal button text)


def test_dashboard_lists_events(client):
    client.post("/auth/dev-login")
    client.post("/events", data={"title": "Birthday bash", "recipients": ""}, follow_redirects=True)
    assert "Birthday bash" in client.get("/").text


def test_bad_image_is_rejected_gracefully(client):
    client.post("/auth/dev-login")
    r = client.post(
        "/events",
        data={"title": "x", "recipients": ""},
        files={"image": ("notes.txt", b"this is not an image", "image/png")},
    )
    assert r.status_code == 400
    assert "image" in r.text.lower()


def test_asset_served_to_owner_only(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={"title": "t", "recipients": ""},
        files={"image": ("c.png", _png_bytes(), "image/png")},
        follow_redirects=True,
    )
    asset_id = _db().execute("SELECT id FROM assets LIMIT 1").fetchone()[0]
    assert client.get(f"/assets/{asset_id}").status_code == 200
    client.get("/auth/logout")
    r = client.get(f"/assets/{asset_id}", follow_redirects=False)
    assert r.status_code == 303  # logged out -> redirected, not served


def test_delete_account_cascades_events_and_recipients(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={"title": "t", "recipients": "a@example.com\nb@example.com"},
        files={"image": ("c.png", _png_bytes(), "image/png")},
        follow_redirects=True,
    )
    client.post("/account/delete")
    db = _db()
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM recipients").fetchone()[0] == 0
