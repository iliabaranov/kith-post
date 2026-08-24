"""Sending invitations and reminders over WhatsApp.

Two things matter more than the happy path here:

* a batch stopped by WhatsApp (reachout timelock, exhausted new-chat quota) must
  stop *and stay stopped*, leaving its recipients queued — every extra attempt
  while restricted makes the account look worse;
* nothing may be added to the message to track anyone. The link is the same
  invitation URL email uses, and it carries no analytics of its own.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kith.config import get_settings
from kith.core import wamessage
from kith.db.models import Event, Recipient, Reminder, User
from kith.services import send as sender
from kith.services import wa_session as link
from kith.services import waha


class FakeWaha:
    """A WAHA that records what it was asked to send, and can be made to refuse."""

    def __init__(self):
        self.status = waha.STATUS_WORKING
        self.sent = []            # (session, to, text, chat_id)
        self.checked = []
        self.exists = True
        self.timelock = None
        self.capping = None
        self.raise_on_send = None     # an exception instance, raised once
        self.raise_after = 0          # ...after this many successful sends
        self.raise_on_check = None

    def _state(self):
        return waha.SessionState(
            name="utest", status=self.status, phone="+15550009999",
            timelock=self.timelock, capping=self.capping,
        )

    def get_session(self, name):
        return self._state()

    def find_session(self, name):
        return self._state()

    def check_exists(self, name, phone):
        self.checked.append(phone)
        if self.raise_on_check:
            raise self.raise_on_check
        return waha.NumberCheck(
            exists=self.exists, chat_id=f"{phone.lstrip('+')}@c.us" if self.exists else None
        )

    def send_text(self, name, to, text, *, link_preview=True, chat_id=None):
        if self.raise_on_send is not None and len(self.sent) >= self.raise_after:
            err, self.raise_on_send = self.raise_on_send, None
            raise err
        self.sent.append((name, to, text, chat_id))
        return {"id": f"false_{to.lstrip('+')}@c.us_MSG{len(self.sent)}"}


@pytest.fixture
def wa(monkeypatch):
    """Channel on, host linked, live sending, fake WAHA. No gap between sends."""
    monkeypatch.setenv("KITH_WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("KITH_WAHA_API_KEY", "test-key")
    monkeypatch.setenv("KITH_SEND_MODE", "live")
    monkeypatch.setenv("KITH_WAHA_SEND_GAP_SECONDS", "0")
    get_settings.cache_clear()
    fake = FakeWaha()
    monkeypatch.setattr(link, "client", lambda settings: fake)
    try:
        from kith.web.app import create_app

        with TestClient(create_app()) as c:
            c.post("/auth/dev-login")
            db, user = _db_and_user()
            user.wa_session = "utest"
            user.wa_status = waha.STATUS_WORKING
            user.wa_number = "+15550009999"
            user.display_name = "Ilia"
            db.commit()
            yield c, fake
    finally:
        get_settings.cache_clear()


def _db_and_user():
    from kith.db.session import make_engine, make_session_factory

    db = make_session_factory(make_engine(get_settings().db_path))()
    return db, db.execute(select(User)).scalars().first()


def _make_event(client, phones, *, emails="", title="Joe's 3rd Birthday", date="2099-06-14"):
    r = client.post(
        "/events",
        data={"title": title, "event_date": date, "event_time": "15:00",
              "recipients": emails, "wa_recipients": phones,
              "block_rsvp": "on", "block_date": "on", "block_time": "on"},
        follow_redirects=False,
    )
    return r.headers["location"].split("/events/")[1].split("?")[0]


def _send(event_id):
    """Run the real send path for this event."""
    db, user = _db_and_user()
    ev = db.get(Event, event_id)
    return db, sender.send_event(db, ev, user, get_settings())


def _recipients(db, event_id):
    return db.execute(
        select(Recipient).where(Recipient.event_id == event_id)
    ).scalars().all()


# --- the happy path -----------------------------------------------------------

def test_sends_one_message_per_recipient_and_marks_them_sent(wa):
    client, fake = wa
    ev = _make_event(client, "Mara <+15551110000>\nSam <+15552220000>")
    db, res = _send(ev)
    assert (res.wa_sent, res.wa_failed, res.wa_blocked) == (2, 0, None)
    assert res.sent == 2
    assert sorted(to for _, to, _, _ in fake.sent) == ["+15551110000", "+15552220000"]
    rows = _recipients(db, ev)
    assert all(r.status == "sent" and r.sent_at and r.wa_message_id for r in rows)


def test_the_message_carries_the_invitation_link_and_no_tracking(wa):
    client, fake = wa
    ev = _make_event(client, "Mara <+15551110000>")
    db, _ = _send(ev)
    token = _recipients(db, ev)[0].token
    _, _, text, _ = fake.sent[0]
    assert f"/i/{token}" in text
    assert text.count("http") == 1, "exactly one link, and it's theirs"
    # No analytics parameters bolted onto the URL.
    link_line = next(ln for ln in text.splitlines() if ln.startswith("http"))
    assert "?" not in link_line and "utm" not in link_line.lower()


def test_the_message_says_who_it_is_from_and_when(wa):
    client, fake = wa
    ev = _make_event(client, "Mara <+15551110000>")
    _send(ev)
    text = fake.sent[0][2]
    assert "Mara" in text and "Ilia" in text
    assert "Joe's 3rd Birthday" in text
    assert "Jun 14" in text and "3:00 pm" in text


def test_it_uses_the_chat_id_whatsapp_resolved(wa):
    client, fake = wa
    ev = _make_event(client, "+15551110000")
    _send(ev)
    assert fake.sent[0][3] == "15551110000@c.us"
    assert fake.checked == ["+15551110000"]


def test_both_channels_go_out_in_one_send(wa, monkeypatch):
    client, fake = wa
    # Keep Gmail out of it: the email half is covered by the existing send tests.
    monkeypatch.setattr(
        "kith.services.gmail.gmail_send",
        lambda *a, **k: {"id": "gm1", "threadId": "th1"},
    )
    ev = _make_event(client, "+15551110000", emails="ali@example.com")
    db, res = _send(ev)
    assert res.wa_sent == 1
    assert res.sent == 2  # one email + one WhatsApp
    assert {r.channel for r in _recipients(db, ev)} == {"email", "whatsapp"}


# --- restrictions stop the batch ---------------------------------------------

def test_a_timelocked_account_sends_nothing_and_keeps_them_queued(wa):
    client, fake = wa
    fake.timelock = waha.Timelock.parse(
        {"isActive": True, "timeEnforcementEnds": 4102444800, "enforcementType": "DEFAULT"}
    )
    ev = _make_event(client, "+15551110000\n+15552220000")
    db, res = _send(ev)
    assert res.wa_blocked == "timelock"
    assert (res.wa_sent, res.wa_failed) == (0, 0)
    assert fake.sent == [], "must not even try while restricted"
    assert all(r.status == "queued" for r in _recipients(db, ev))
    # ...and the end date is remembered so the UI can explain the pause.
    _, user = _db_and_user()
    assert user.wa_timelock_until is not None


def test_a_timelock_hit_mid_batch_stops_the_rest(wa):
    client, fake = wa
    ev = _make_event(client, "+15551110000\n+15552220000\n+15553330000")
    fake.raise_on_send, fake.raise_after = waha.Timelocked(None), 1  # trips on #2
    db, res = _send(ev)
    assert res.wa_blocked == "timelock"
    assert res.wa_sent == 1, "the first one got through"
    assert len(fake.sent) == 1, "and we stopped instead of hammering"
    statuses = sorted(r.status for r in _recipients(db, ev))
    assert statuses == ["queued", "queued", "sent"]


def test_an_exhausted_quota_stops_the_batch(wa):
    client, fake = wa
    fake.capping = waha.Capping.parse(
        {"cappingStatus": "CAPPED", "totalQuota": 100, "usedQuota": 100,
         "cycleEnd": 4102444800}
    )
    ev = _make_event(client, "+15551110000")
    db, res = _send(ev)
    assert res.wa_blocked == "capped" and fake.sent == []
    assert all(r.status == "queued" for r in _recipients(db, ev))


def test_a_dead_pairing_stops_the_batch(wa):
    client, fake = wa
    fake.status = waha.STATUS_FAILED
    ev = _make_event(client, "+15551110000")
    db, res = _send(ev)
    assert res.wa_blocked == "not-linked" and fake.sent == []
    assert all(r.status == "queued" for r in _recipients(db, ev))


def test_an_unreachable_waha_stops_the_batch_without_crashing(wa, monkeypatch):
    client, fake = wa

    def boom(name):
        raise waha.WahaTimeout("WAHA is down")

    monkeypatch.setattr(fake, "get_session", boom)
    ev = _make_event(client, "+15551110000")
    db, res = _send(ev)
    assert res.wa_blocked == "unavailable"
    assert all(r.status == "queued" for r in _recipients(db, ev))


def test_the_host_is_told_why_a_batch_stopped(wa):
    client, fake = wa
    fake.timelock = waha.Timelock.parse(
        {"isActive": True, "timeEnforcementEnds": 4102444800, "enforcementType": "DEFAULT"}
    )
    ev = _make_event(client, "+15551110000")
    body = client.post(f"/events/{ev}/send", follow_redirects=True).text
    assert "paused new conversations" in body
    assert "still queued" in body
    assert "Re-linking won" in body  # ...won't help — apostrophe is escaped


# --- individual failures ------------------------------------------------------

def test_a_number_not_on_whatsapp_costs_only_that_recipient(wa):
    client, fake = wa
    fake.exists = False
    ev = _make_event(client, "+15551110000\n+15552220000")
    db, res = _send(ev)
    assert res.wa_failed == 2 and res.wa_sent == 0
    assert res.wa_blocked is None, "one bad number is not an account problem"
    assert fake.sent == []
    assert all(r.status == "queued" for r in _recipients(db, ev))


def test_a_failed_existence_check_does_not_block_the_send(wa):
    """A flaky pre-check is not an answer about the number."""
    client, fake = wa
    fake.raise_on_check = waha.WahaTimeout("check timed out")
    ev = _make_event(client, "+15551110000")
    db, res = _send(ev)
    assert res.wa_sent == 1
    assert fake.sent[0][3] is None  # no resolved chat id, so it derives one


def test_one_recipients_error_does_not_stop_the_others(wa):
    client, fake = wa
    ev = _make_event(client, "+15551110000\n+15552220000")
    fake.raise_on_send = waha.WahaError("transient")
    db, res = _send(ev)
    assert (res.wa_sent, res.wa_failed) == (1, 1)
    assert res.wa_blocked is None


# --- send modes ---------------------------------------------------------------

def test_dry_run_writes_the_text_and_calls_nothing(wa, monkeypatch):
    client, fake = wa
    monkeypatch.setenv("KITH_SEND_MODE", "dry-run")
    get_settings.cache_clear()
    ev = _make_event(client, "Mara <+15551110000>")
    db, res = _send(ev)
    assert res.wa_sent == 1 and fake.sent == [] and fake.checked == []
    out = get_settings().outbox_dir / ev / "whatsapp"
    files = list(out.glob("*.txt"))
    assert len(files) == 1
    body = files[0].read_text()
    assert "To: +15551110000" in body
    assert "Joe's 3rd Birthday" in body and "/i/" in body


def test_self_only_sends_to_the_host_not_the_guest(wa, monkeypatch):
    client, fake = wa
    monkeypatch.setenv("KITH_SEND_MODE", "self-only")
    get_settings.cache_clear()
    ev = _make_event(client, "+15551110000")
    _send(ev)
    assert [to for _, to, _, _ in fake.sent] == ["+15550009999"]  # the host's own number


# --- reminders ----------------------------------------------------------------

def _due_reminder(db, event_id):
    """Fire whatever reminder the scheduler planned, as if it were due."""
    from datetime import UTC, datetime, timedelta

    from kith.services import scheduler

    rem = db.execute(
        select(Reminder).where(Reminder.event_id == event_id)
    ).scalars().first()
    assert rem is not None, "sending should have scheduled a nudge"
    rem.scheduled_for = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    return rem, scheduler.send_one_reminder(db, rem, get_settings())


def test_a_reminder_goes_to_the_same_whatsapp_chat(wa):
    client, fake = wa
    ev = _make_event(client, "Mara <+15551110000>")
    client.post(f"/events/{ev}/send", follow_redirects=False)
    db, _ = _db_and_user()
    fake.sent.clear()
    rem, ok = _due_reminder(db, ev)
    assert ok and rem.status == "sent"
    _, to, text, _ = fake.sent[0]
    assert to == "+15551110000"
    assert "gentle nudge" in text
    assert "Joe's 3rd Birthday" in text


def test_a_timelock_holds_a_reminder_instead_of_losing_it(wa):
    client, fake = wa
    ev = _make_event(client, "+15551110000")
    client.post(f"/events/{ev}/send", follow_redirects=False)
    db, _ = _db_and_user()
    fake.sent.clear()
    fake.timelock = waha.Timelock.parse(
        {"isActive": True, "timeEnforcementEnds": 4102444800, "enforcementType": "DEFAULT"}
    )
    rem, ok = _due_reminder(db, ev)
    assert not ok
    assert rem.status == "pending", "held for the next tick, not dropped"
    assert rem.sent_at is None
    assert fake.sent == []


def test_a_dry_run_reminder_is_written_not_sent(wa, monkeypatch):
    client, fake = wa
    monkeypatch.setenv("KITH_SEND_MODE", "dry-run")
    get_settings.cache_clear()
    ev = _make_event(client, "+15551110000")
    client.post(f"/events/{ev}/send", follow_redirects=False)
    db, _ = _db_and_user()
    rem, ok = _due_reminder(db, ev)
    assert ok and fake.sent == []
    written = list((get_settings().outbox_dir / ev / "whatsapp" / "reminders").glob("*.txt"))
    assert len(written) == 1 and "gentle nudge" in written[0].read_text()


# --- copy ---------------------------------------------------------------------

def test_a_dateless_card_just_omits_the_date():
    text = wamessage.invite_text(
        title="Season's greetings", host_name="Ilia",
        view_url="https://example.com/i/abc", when=None, rsvp=False,
    )
    assert "None" not in text
    assert "Have a look:" in text


def test_an_unnamed_recipient_still_gets_a_greeting():
    text = wamessage.invite_text(
        title="Party", host_name="Ilia", view_url="https://example.com/i/abc",
        recipient_name=None,
    )
    assert text.startswith("Hi! It's Ilia.")


def test_the_note_from_a_resend_is_included():
    text = wamessage.invite_text(
        title="Party", host_name="Ilia", view_url="https://example.com/i/abc",
        note="The time has moved to 4pm.",
    )
    assert "The time has moved to 4pm." in text


# --- privacy ------------------------------------------------------------------

def test_deleting_an_account_unlinks_whatsapp_first(wa, monkeypatch):
    """The pairing lives in WAHA's volume, so deleting the user must not orphan
    live WhatsApp credentials."""
    client, fake = wa
    unlinked = []
    monkeypatch.setattr(fake, "unlink", lambda name: unlinked.append(name), raising=False)
    client.post("/account/delete", follow_redirects=False)
    assert unlinked == ["utest"]
    db, user = _db_and_user()
    assert user is None


def test_the_export_includes_the_link_and_contact_numbers(wa):
    client, fake = wa
    from kith.services import contacts as book

    db, user = _db_and_user()
    book.add_contact(db, user.id, "mara@example.com", "Mara", phone="+15551110000")
    data = client.get("/account/export").json()
    assert data["whatsapp"]["linked"] is True
    assert data["whatsapp"]["number"] == "+15550009999"
    assert data["contacts"][0]["phone"] == "+15551110000"
