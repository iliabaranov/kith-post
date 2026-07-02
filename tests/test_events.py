"""Compose-a-card flow (G2), via the dev-login session."""

import io
import sqlite3

from PIL import Image

from kith.config import get_settings


def _png_bytes(size=(60, 40), color="orange") -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


def _png_file():
    return {"image": ("card.png", _png_bytes(), "image/png")}


def _db():
    return sqlite3.connect(get_settings().db_path)


def _event_id():
    return _db().execute("SELECT id FROM events LIMIT 1").fetchone()[0]


def test_new_event_form_requires_login(client):
    assert client.get("/events/new", follow_redirects=False).status_code == 303


def test_preview_requires_login(client):
    assert client.get("/events/anything/preview", follow_redirects=False).status_code == 303


def test_create_redirects_to_detail_with_recipient_count(client):
    client.post("/auth/dev-login")
    r = client.post(
        "/events",
        data={"title": "Maya turns five!", "recipients": "a@example.com\nb@example.com",
              "block_rsvp": "on"},
        files=_png_file(),
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "Maya turns five!" in r.text
    assert "2 recipients" in r.text
    assert "Preview invitation" in r.text


def test_preview_is_the_interactive_invite(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={
            "title": "Maya turns five!", "message": "Come play.",
            "event_date": "2026-05-04", "event_time": "3:00 pm", "event_end_time": "5:00 pm",
            "location": "14 Linden St", "signoff": "love, Mara & Tom",
            "recipients": "a@example.com",
            "block_message": "on", "block_date": "on", "block_time": "on",
            "block_location": "on", "block_rsvp": "on", "block_headcount": "on",
        },
        files=_png_file(),
        follow_redirects=True,
    )
    p = client.get(f"/events/{_event_id()}/preview")
    assert p.status_code == 200
    assert "Maya turns five!" in p.text
    assert "I'll be there" in p.text       # interactive RSVP
    assert "/assets/" in p.text            # real uploaded image
    assert "invite.js" in p.text           # interactive script wired
    assert "envelope" in p.text            # envelope intro present
    assert "5:00 pm" in p.text             # end time renders (start – end)
    assert "love, Mara" in p.text          # custom signoff (& escaped)
    assert "How many are coming" in p.text  # headcount step present


def test_form_has_disclosure_wiring(client):
    """Checkboxes sit up top and each reveals its field; collapse is JS-gated."""
    client.post("/auth/dev-login")
    f = client.get("/events/new").text
    assert "What to include" in f
    for tid in ("t-message", "t-date", "t-time", "t-location", "t-headcount"):
        assert f'id="{tid}"' in f and f'data-reveal="{tid}"' in f
    assert "checked>" not in f  # a new card starts blank — no box pre-selected
    assert 'classList.add("js")' in client.get("/").text  # no-JS keeps fields visible


def test_time_block_requires_a_date(client):
    """A bare time is meaningless on an invite: time only counts with a date."""
    import json
    client.post("/auth/dev-login")
    # time ticked but no date -> time block suppressed server-side
    client.post(
        "/events",
        data={"title": "x", "recipients": "", "event_time": "15:00", "block_time": "on"},
        follow_redirects=True,
    )
    blocks = json.loads(_db().execute("SELECT blocks FROM events LIMIT 1").fetchone()[0])
    assert blocks["time"] is False and blocks["date"] is False


def test_headcount_max_caps_the_stepper(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={"title": "Dinner", "recipients": "a@example.com",
              "block_rsvp": "on", "block_headcount": "on", "headcount_max": "4"},
        follow_redirects=True,
    )
    eid = _event_id()
    assert _db().execute("SELECT headcount_max FROM events LIMIT 1").fetchone()[0] == 4
    p = client.get(f"/events/{eid}/preview")
    assert 'data-max="4"' in p.text   # JS caps the + at this
    assert "up to 4" in p.text         # guest-facing cue


def test_add_to_calendar_on_dated_event(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={"title": "Maya turns five!", "event_date": "2026-05-04",
              "event_time": "15:00", "event_end_time": "17:00", "timezone": "America/Toronto",
              "recipients": "a@example.com",
              "block_date": "on", "block_time": "on", "block_rsvp": "on"},
        follow_redirects=True,
    )
    eid = _event_id()
    p = client.get(f"/events/{eid}/preview")
    assert "calendar.google.com" in p.text     # Google link present
    assert 'id="addcal"' in p.text             # revealed after accept
    assert "3:00 pm" in p.text                 # HH:MM formatted for display
    # the .ics download works
    ics = client.get(f"/events/{eid}/calendar.ics")
    assert ics.status_code == 200
    assert "calendar" in ics.headers["content-type"]
    assert "BEGIN:VCALENDAR" in ics.text
    assert "Maya turns five!" in ics.text


def test_no_calendar_without_a_date(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={"title": "Open house", "block_rsvp": "on", "recipients": ""},
        follow_redirects=True,
    )
    eid = _event_id()
    assert "calendar.google.com" not in client.get(f"/events/{eid}/preview").text
    assert client.get(f"/events/{eid}/calendar.ics", follow_redirects=False).status_code == 303


def test_holiday_card_preview_has_no_rsvp(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={"title": "Winter cheer", "message": "Happy holidays!",
              "block_message": "on", "recipients": ""},
        follow_redirects=True,
    )
    p = client.get(f"/events/{_event_id()}/preview")
    assert "Winter cheer" in p.text
    assert "I'll be there" not in p.text   # RSVP off -> plain card


def test_signoff_defaults_to_display_name(client):
    client.post("/auth/dev-login")  # Dev User
    client.post("/events", data={"title": "x", "recipients": ""}, follow_redirects=True)
    p = client.get(f"/events/{_event_id()}/preview")
    assert "Dev User" in p.text             # signoff fell back to the host's name


def test_recipient_emails_encrypted_at_rest(client):
    client.post("/auth/dev-login")
    client.post(
        "/events", data={"title": "x", "recipients": "secret@example.com"}, follow_redirects=True
    )
    raw = _db().execute("SELECT email FROM recipients LIMIT 1").fetchone()[0]
    assert "@" not in raw
    assert raw.startswith("gAAAA")


def test_dashboard_lists_events(client):
    client.post("/auth/dev-login")
    client.post("/events", data={"title": "Birthday bash", "recipients": ""}, follow_redirects=True)
    assert "Birthday bash" in client.get("/").text


def test_event_page_shows_rsvp_stats_and_list(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={"title": "Party", "recipients": "a@example.com\nb@example.com",
              "block_rsvp": "on", "block_headcount": "on"},
        follow_redirects=True,
    )
    toks = [r[0] for r in _db().execute("SELECT token FROM recipients").fetchall()]
    client.post(f"/i/{toks[0]}/rsvp", data={"response": "coming", "adults": "2"})
    client.post(f"/i/{toks[1]}/rsvp", data={"response": "declined"})
    page = client.get(f"/events/{_event_id()}").text
    assert "chip-coming" in page and "chip-declined" in page  # per-recipient chips
    assert "2 guests" in page  # party size flows into the guest tally
    assert "a@example.com" in page and "b@example.com" in page


def test_no_rsvp_tally_on_a_plain_card(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={"title": "Holiday", "recipients": "a@example.com",
              "message": "hi", "block_message": "on"},
        follow_redirects=True,
    )
    assert 'class="stats"' not in client.get(f"/events/{_event_id()}").text


def test_delete_event_removes_it_and_recipients(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={"title": "Gone soon", "recipients": "a@example.com\nb@example.com"},
        files=_png_file(),
        follow_redirects=True,
    )
    eid = _event_id()
    r = client.post(f"/events/{eid}/delete", follow_redirects=True)
    assert r.status_code == 200
    assert "Gone soon" not in r.text
    db = _db()
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM recipients").fetchone()[0] == 0


def test_delete_requires_login(client):
    client.post("/auth/dev-login")
    client.post("/events", data={"title": "Keep me", "recipients": ""}, follow_redirects=True)
    eid = _event_id()
    client.get("/auth/logout")
    assert client.post(f"/events/{eid}/delete", follow_redirects=False).status_code == 303
    client.post("/auth/dev-login")
    assert _db().execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1  # still there


def test_past_event_is_flagged(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={"title": "Old party", "recipients": "",
              "event_date": "2020-01-01", "block_date": "on"},
        follow_redirects=True,
    )
    home = client.get("/").text
    assert "event passed" in home and "is-past" in home


def test_future_event_is_not_flagged(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={"title": "Future fest", "recipients": "",
              "event_date": "2099-12-31", "block_date": "on"},
        follow_redirects=True,
    )
    home = client.get("/").text
    assert "Future fest" in home and "event passed" not in home


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
        "/events", data={"title": "t", "recipients": ""}, files=_png_file(), follow_redirects=True
    )
    asset_id = _db().execute("SELECT id FROM assets LIMIT 1").fetchone()[0]
    assert client.get(f"/assets/{asset_id}").status_code == 200
    client.get("/auth/logout")
    assert client.get(f"/assets/{asset_id}", follow_redirects=False).status_code == 303


def test_delete_account_cascades_events_and_recipients(client):
    client.post("/auth/dev-login")
    client.post(
        "/events",
        data={"title": "t", "recipients": "a@example.com\nb@example.com"},
        files=_png_file(),
        follow_redirects=True,
    )
    client.post("/account/delete")
    db = _db()
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM recipients").fetchone()[0] == 0
