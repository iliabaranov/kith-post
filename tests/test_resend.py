"""P6: changing date/time/location on a sent event prompts an optional re-send that
re-collects RSVPs and reschedules reminders."""

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


def _eid():
    return _db().execute("SELECT id FROM events LIMIT 1").fetchone()[0]


def _rows():
    return _db().execute(
        "SELECT token, status, rsvp_at, party_size FROM recipients"
    ).fetchall()


def _rem_counts():
    return dict(_db().execute("SELECT status, COUNT(*) FROM reminders GROUP BY status").fetchall())


def _form(**extra):
    d = {"title": "Party", "recipients": "a@example.com",
         "block_rsvp": "on", "block_date": "on", "event_date": "2030-01-01"}
    d.update(extra)
    return d


def _create(client):
    client.post("/auth/dev-login")
    client.post("/events", data=_form(), files=_png_file(), follow_redirects=True)
    return _eid()


def _create_and_send(client):
    eid = _create(client)
    client.post(f"/events/{eid}/send")
    return eid


def test_date_change_prompts_resend(client):
    eid = _create_and_send(client)
    r = client.post(f"/events/{eid}", data=_form(event_date="2030-02-02"), follow_redirects=True)
    assert "re-collect RSVPs" in r.text


def test_no_prompt_when_details_unchanged(client):
    eid = _create_and_send(client)
    r = client.post(f"/events/{eid}", data=_form(title="New Title"), follow_redirects=True)
    assert "re-collect RSVPs" not in r.text


def test_no_prompt_when_nothing_sent(client):
    eid = _create(client)  # created but never sent
    r = client.post(f"/events/{eid}", data=_form(event_date="2030-03-03"), follow_redirects=True)
    assert "re-collect RSVPs" not in r.text


def test_resend_resets_rsvp_and_reschedules(client):
    eid = _create_and_send(client)
    tok = _rows()[0][0]
    client.post(f"/i/{tok}/rsvp", data={"response": "coming", "party_size": "3"})
    assert _rows()[0][1] == "coming"

    client.post(f"/events/{eid}/resend")
    tok2, status2, rsvp_at2, party2 = _rows()[0]
    assert tok2 == tok            # token preserved (stable link + threading)
    assert status2 == "sent"      # re-sent (dry-run) → back to sent
    assert rsvp_at2 is None       # prior RSVP cleared
    assert party2 is None
    assert _rem_counts().get("pending", 0) > 0  # reminders rescheduled

    files = list((get_settings().data_dir / "outbox" / eid).glob("*.eml"))
    assert files and "details have changed" in files[0].read_text()
