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
        self.phone = "+15550009999"    # the host's own linked number
        self.sent = []            # (session, to, text, chat_id)
        self.images = []          # (session, to, {caption, mimetype, bytes})
        self.replies = []         # reply_to per send_text call
        self.checked = []
        self.exists = True
        self.timelock = None
        self.capping = None
        self.raise_on_send = None     # an exception instance, raised once
        self.raise_after = 0          # ...after this many successful sends
        self.raise_on_check = None

    def _state(self):
        return waha.SessionState(
            name="utest", status=self.status, phone=self.phone,
            timelock=self.timelock, capping=self.capping,
        )

    def get_session(self, name):
        return self._state()

    def find_session(self, name):
        return self._state()

    def ensure_webhooks(self, name):
        return False

    def check_exists(self, name, phone):
        self.checked.append(phone)
        if self.raise_on_check:
            raise self.raise_on_check
        return waha.NumberCheck(
            exists=self.exists, chat_id=f"{phone.lstrip('+')}@c.us" if self.exists else None
        )

    def send_image(self, name, to, image, *, mimetype="image/jpeg", caption="",
                   filename="card.jpg", chat_id=None, reply_to=None):
        if self.raise_on_send is not None and len(self.images) >= self.raise_after:
            err, self.raise_on_send = self.raise_on_send, None
            raise err
        self.images.append((name, to, {
            "caption": caption, "mimetype": mimetype, "bytes": len(image),
            "filename": filename, "chat_id": chat_id,
        }))
        return {"id": f"false_{to.lstrip('+')}@c.us_IMG{len(self.images)}"}

    def send_text(self, name, to, text, *, link_preview=True, chat_id=None,
                  reply_to=None):
        if self.raise_on_send is not None and len(self.sent) >= self.raise_after:
            err, self.raise_on_send = self.raise_on_send, None
            raise err
        self.sent.append((name, to, text, chat_id))
        self.replies.append(reply_to)
        return {"id": f"false_{to.lstrip('+')}@c.us_MSG{len(self.sent)}"}


@pytest.fixture
def wa(monkeypatch):
    """Channel on, host linked, live sending, fake WAHA. No gap between sends."""
    monkeypatch.setenv("KITH_WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("KITH_WAHA_API_KEY", "test-key")
    monkeypatch.setenv("KITH_SEND_MODE", "live")
    monkeypatch.setenv("KITH_WAHA_SEND_GAP_MIN_SECONDS", "0")
    monkeypatch.setenv("KITH_WAHA_SEND_GAP_MAX_SECONDS", "0")
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


def test_self_only_without_a_stored_number_fails_that_recipient(wa, monkeypatch):
    """A linked session always reports its number, but if it somehow hasn't, we
    report the recipient rather than handing WAHA an empty chat id."""
    client, fake = wa
    monkeypatch.setenv("KITH_SEND_MODE", "self-only")
    get_settings.cache_clear()
    # Clearing the cached number isn't enough: the pre-flight refreshes it from
    # the live session (which is the right behaviour), so the session itself has
    # to be the one reporting no number.
    fake.phone = None
    db, user = _db_and_user()
    user.wa_number = None
    db.commit()
    ev = _make_event(client, "+15551110000")
    db, res = _send(ev)
    assert (res.wa_sent, res.wa_failed) == (0, 1)
    assert res.wa_blocked is None and fake.sent == []


def test_an_unlinked_host_is_unaffected_by_the_channel_being_on(monkeypatch):
    """The invariant: WhatsApp is optional per user, not just per deployment.

    With the channel enabled server-wide but this host never having linked (or
    even acknowledged the warning), an email-only card must send exactly as
    before — no WhatsApp call, no gate, nothing withheld.
    """
    monkeypatch.setenv("KITH_WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("KITH_WAHA_API_KEY", "test-key")
    monkeypatch.setenv("KITH_SEND_MODE", "live")
    get_settings.cache_clear()
    # Any WAHA call at all is a failure of the invariant.
    def forbidden(settings):
        raise AssertionError("an unlinked host must never reach WAHA")

    monkeypatch.setattr(link, "client", forbidden)
    monkeypatch.setattr(
        "kith.services.gmail.gmail_send", lambda *a, **k: {"id": "gm1", "threadId": "th1"}
    )
    try:
        from kith.web.app import create_app

        with TestClient(create_app()) as c:
            c.post("/auth/dev-login")
            db, user = _db_and_user()
            user.refresh_token, user.display_name = "tok", "Ilia"
            db.commit()
            assert user.wa_session is None and user.wa_risk_ack_at is None

            ev = _make_event(c, "", emails="ali@example.com")
            db, res = _send(ev)
            assert (res.sent, res.failed) == (1, 0)
            assert (res.wa_sent, res.wa_failed, res.wa_blocked) == (0, 0, None)
            assert all(r.status == "sent" for r in _recipients(db, ev))
    finally:
        get_settings.cache_clear()


def test_the_card_is_sent_as_the_image_with_the_words_as_its_caption(wa, tmp_path):
    client, fake = wa
    img = tmp_path / "card.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"fake jpeg" * 100)
    ev = _make_event(client, "Mara <+15551110000>")
    db, user = _db_and_user()
    from kith.db.models import Asset, Event

    a = Asset(user_id=user.id, sha256="x", mime="image/jpeg", full_path=str(img),
              inline_path=str(img), width=600, height=800, bytes=img.stat().st_size)
    db.add(a)
    db.commit()
    event = db.get(Event, ev)
    event.asset_id = a.id
    db.commit()

    _, res = _send(ev)
    assert res.wa_sent == 1
    assert len(fake.images) == 1 and fake.sent == [], "should be one image, not a bare text"
    _, _, payload = fake.images[0]
    assert payload["caption"].startswith("Hi Mara")
    assert "/i/" in payload["caption"]
    assert payload["mimetype"] == "image/jpeg"
    assert payload["bytes"] == img.stat().st_size


def test_a_card_with_no_picture_is_still_sent_as_text(wa):
    client, fake = wa
    ev = _make_event(client, "+15551110000")
    _, res = _send(ev)
    assert res.wa_sent == 1 and fake.images == [] and len(fake.sent) == 1


def test_a_missing_image_file_does_not_break_the_send(wa):
    """Assets outlive their files: the retention sweep drops the full-res copy,
    and older rows can be missing the inline one. Reading it unguarded took the
    whole send down — both channels."""
    client, fake = wa
    ev = _make_event(client, "+15551110000")
    db, user = _db_and_user()
    from kith.db.models import Asset, Event

    a = Asset(user_id=user.id, sha256="x", mime="image/jpeg",
              full_path="/nonexistent/gone.jpg", inline_path="/nonexistent/gone.jpg",
              width=1, height=1, bytes=0)
    db.add(a)
    db.commit()
    event = db.get(Event, ev)
    event.asset_id = a.id
    db.commit()

    _, res = _send(ev)
    assert res.wa_sent == 1, "the words and the link still go out"
    assert len(fake.sent) == 1 and fake.images == []


def test_a_reminder_quotes_the_invitation_it_is_nudging_about(wa):
    client, fake = wa
    ev = _make_event(client, "+15551110000")
    client.post(f"/events/{ev}/send", follow_redirects=False)
    db, _ = _db_and_user()
    rows = _recipients(db, ev)
    original_id = rows[0].wa_message_id
    assert original_id
    fake.sent.clear()
    fake.replies.clear()
    _due_reminder(db, ev)
    assert fake.replies == [original_id], "the nudge should reply to the invitation"


# --- pacing -------------------------------------------------------------------

def test_the_gap_is_random_inside_the_configured_range():
    """A fixed gap is as machine-like as no gap, only slower."""
    import random as _random

    from kith.config import Settings

    s = Settings(waha_send_gap_min_seconds=5, waha_send_gap_max_seconds=20)
    gaps = [sender.next_send_gap(s, _random.Random(seed)) for seed in range(200)]
    assert all(5 <= g <= 20 for g in gaps)
    assert len(set(round(g, 3) for g in gaps)) > 150, "gaps should not repeat"
    # Spread across the range, not clustered at one end.
    assert min(gaps) < 8 and max(gaps) > 17


def test_the_gap_can_be_switched_off():
    from kith.config import Settings

    s = Settings(waha_send_gap_min_seconds=0, waha_send_gap_max_seconds=0)
    assert sender.next_send_gap(s) == 0.0


def test_a_reversed_range_does_not_explode():
    from kith.config import Settings

    s = Settings(waha_send_gap_min_seconds=30, waha_send_gap_max_seconds=5)
    assert sender.next_send_gap(s) == 30.0     # clamped to the lower bound


def test_the_default_range_is_five_to_twenty():
    from kith.config import Settings

    s = Settings()
    assert (s.waha_send_gap_min_seconds, s.waha_send_gap_max_seconds) == (5.0, 20.0)


def test_sends_actually_wait_between_recipients(wa, monkeypatch):
    """The pause has to happen between messages, and not before the first or
    after the last."""
    client, fake = wa
    monkeypatch.setenv("KITH_WAHA_SEND_GAP_MIN_SECONDS", "5")
    monkeypatch.setenv("KITH_WAHA_SEND_GAP_MAX_SECONDS", "20")
    get_settings.cache_clear()
    slept: list[float] = []
    monkeypatch.setattr("kith.services.send.time.sleep", lambda s: slept.append(s))

    ev = _make_event(client, "+15551110000\n+15552220000\n+15553330000")
    _, res = _send(ev)
    assert res.wa_sent == 3
    assert len(slept) == 2, "three sends means two pauses"
    assert all(5 <= s <= 20 for s in slept)


def test_a_single_recipient_is_not_delayed(wa, monkeypatch):
    client, fake = wa
    monkeypatch.setenv("KITH_WAHA_SEND_GAP_MIN_SECONDS", "5")
    get_settings.cache_clear()
    slept: list[float] = []
    monkeypatch.setattr("kith.services.send.time.sleep", lambda s: slept.append(s))
    _send(_make_event(client, "+15551110000"))
    assert slept == []


def test_a_dry_run_does_not_wait(wa, monkeypatch):
    client, fake = wa
    monkeypatch.setenv("KITH_SEND_MODE", "dry-run")
    monkeypatch.setenv("KITH_WAHA_SEND_GAP_MIN_SECONDS", "5")
    get_settings.cache_clear()
    slept: list[float] = []
    monkeypatch.setattr("kith.services.send.time.sleep", lambda s: slept.append(s))
    _send(_make_event(client, "+15551110000\n+15552220000"))
    assert slept == [], "composing to the outbox has nothing to pace"


# --- the batch runs off the request path --------------------------------------

def test_the_request_does_not_wait_for_the_whatsapp_batch(wa):
    """At 5-20s a family-sized list takes minutes, which is longer than an HTTP
    request should live — and longer than the tunnel holds one open."""
    client, fake = wa
    ev = _make_event(client, "+15551110000\n+15552220000")
    db, user = _db_and_user()
    event = db.get(Event, ev)
    res = sender.send_event(db, event, user, get_settings(), wa_defer=True)
    assert res.wa_pending == 2
    assert (res.wa_sent, res.wa_failed) == (0, 0)
    assert fake.sent == [] and fake.images == []
    assert all(r.status == "queued" for r in _recipients(db, ev))


def test_the_deferred_batch_then_sends_and_schedules_reminders(wa):
    from kith.db.session import make_engine, make_session_factory

    client, fake = wa
    ev = _make_event(client, "+15551110000\n+15552220000")
    factory = make_session_factory(make_engine(get_settings().db_path))
    sender.send_whatsapp_batch(factory, ev, get_settings())
    db, _ = _db_and_user()
    rows = _recipients(db, ev)
    assert all(r.status == "sent" and r.sent_at for r in rows)
    assert len(fake.sent) == 2
    # Reminders hang off sent_at, so they can only be scheduled once the batch ran.
    from kith.db.models import Reminder

    assert db.execute(select(Reminder).where(Reminder.event_id == ev)).scalars().all()


def test_two_batches_for_one_card_cannot_overlap(wa, monkeypatch):
    """A host pressing Send twice during a paced batch would double-message
    people — rude, and exactly what gets an account limited."""
    from kith.db.session import make_engine, make_session_factory

    client, fake = wa
    ev = _make_event(client, "+15551110000")
    factory = make_session_factory(make_engine(get_settings().db_path))

    seen = []
    real = sender._send_whatsapp

    def reentrant(db, event, user, settings, recipients, **kw):
        # While the first batch is mid-flight, a second must decline.
        seen.append(sender.wa_batch_running(event.id))
        sender.send_whatsapp_batch(factory, event.id, settings)
        return real(db, event, user, settings, recipients, **kw)

    monkeypatch.setattr(sender, "_send_whatsapp", reentrant)
    sender.send_whatsapp_batch(factory, ev, get_settings())
    assert seen == [True]
    assert len(fake.sent) == 1, "the second batch must not have sent anything"


def test_the_page_says_the_batch_is_under_way(wa):
    client, fake = wa
    ev = _make_event(client, "+15551110000")
    body = client.post(f"/events/{ev}/send", follow_redirects=True).text
    assert "Sending the WhatsApp invitations now" in body


def test_a_timelock_is_still_explained_after_the_batch_runs(wa):
    """The reason is found in the background now, so the page has to work it out
    from the account's state rather than from the redirect."""
    client, fake = wa
    fake.timelock = waha.Timelock.parse(
        {"isActive": True, "timeEnforcementEnds": 4102444800, "enforcementType": "DEFAULT"}
    )
    ev = _make_event(client, "+15551110000")
    body = client.post(f"/events/{ev}/send", follow_redirects=True).text
    assert "paused new conversations" in body
    assert "still queued" in body
