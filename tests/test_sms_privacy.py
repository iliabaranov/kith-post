"""Account export and delete, for the SMS channel's data.

The promise on the account page is "download my data" and "delete my account and
all your data". Both have to be true of the columns the SMS channel added, and
the one that matters most is the opt-out: a record that someone asked not to be
texted is exactly the fact they are most entitled to see in an export.

Delete already cascades through foreign keys, so most of this asserts that the
cascade is real rather than assumed — that it reaches the outbox on disk, and
that it deliberately does NOT reach the opt-out log, which has to outlive the
account or the same number is textable again the moment someone signs back in.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from kith.config import get_settings
from kith.db.models import Contact, Event, Recipient, SmsOptOutEvent, User
from kith.services.contacts import phone_hash

GUEST_SMS = "+15551110000"
GUEST_WA = "+15552220000"


@pytest.fixture
def sms_client(monkeypatch):
    for k, v in {
        "KITH_SMS_ENABLED": "true", "KITH_SMS_PROVIDER": "twilio",
        "KITH_SMS_TWILIO_ACCOUNT_SID": "AC1", "KITH_SMS_TWILIO_AUTH_TOKEN": "tok",
        "KITH_SMS_TWILIO_FROM": "+15550001234",
        "KITH_WHATSAPP_ENABLED": "true", "KITH_WAHA_API_KEY": "test-key",
    }.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    try:
        from kith.web.app import create_app

        with TestClient(create_app()) as c:
            c.post("/auth/dev-login")
            yield c
    finally:
        get_settings.cache_clear()


@pytest.fixture
def off_client(monkeypatch):
    """A fresh app with the channel off.

    Not the shared `client` fixture: /account/export renders from a settings
    snapshot captured when the app was built (app.py closes over `settings`),
    so the module-level app carries whatever environment was set the first time
    something imported it.
    """
    for k, v in {
        "KITH_SMS_ENABLED": "false", "KITH_SMS_PROVIDER": "none",
        "KITH_SMS_TWILIO_ACCOUNT_SID": "", "KITH_SMS_TWILIO_AUTH_TOKEN": "",
        "KITH_SMS_TWILIO_FROM": "",
    }.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    try:
        from kith.web.app import create_app

        with TestClient(create_app()) as c:
            c.post("/auth/dev-login")
            yield c
    finally:
        get_settings.cache_clear()


def _db():
    from kith.db.session import make_engine, make_session_factory

    return make_session_factory(make_engine(get_settings().db_path))()


def _seed(client):
    """A card sent by text and by WhatsApp, with a delivery receipt on the SMS
    guest, an opt-out recorded for their number, and a contact for them —
    written the way it lands in production: the outbox from a dry-run send, the
    opt-out as a row in the log rather than a flag on anything the host owns."""
    from datetime import UTC, datetime

    from kith.services import contacts as book

    db = _db()
    user = db.execute(select(User)).scalars().first()
    book.add_contact(db, user.id, "", "Mara", phone=GUEST_SMS)
    db.commit()

    r = client.post(
        "/events",
        data={"title": "Joe's 3rd Birthday", "event_date": "2099-06-14",
              "recipients": "ali@example.com", "wa_recipients": GUEST_WA,
              "sms_recipients": GUEST_SMS, "block_rsvp": "on", "block_date": "on"},
        follow_redirects=False,
    )
    ev = r.headers["location"].split("/events/")[1].split("?")[0]

    # A dry-run send, so the outbox on disk holds the guests' addresses and texts.
    client.post(f"/events/{ev}/send", follow_redirects=False)
    from kith.services import send as sender

    assert sender.wait_for_batches(timeout=30)

    # The STOP arrives after the invitation went out, as it would in life.
    db2 = _db()
    db2.add(SmsOptOutEvent(
        phone_hash=phone_hash(GUEST_SMS), kind="stop", source="twilio", message_sid="SM_in_1",
    ))
    for row in db2.execute(select(Recipient).where(Recipient.event_id == ev)).scalars():
        if row.phone == GUEST_SMS:
            row.status = "sent"
            row.sms_message_id = "SM_abc"
            row.sms_delivered_at = datetime.now(UTC)
            row.note = "can't make it, sorry!"
    db2.commit()
    return ev


# --- export --------------------------------------------------------------------

def test_the_export_carries_the_guest_list_at_all(sms_client):
    """It didn't before. An export of a card with no recipients is an export of
    the shell of the card, not of the card."""
    _seed(sms_client)
    data = sms_client.get("/account/export").json()
    recipients = data["events"][0]["recipients"]
    assert len(recipients) == 3
    assert {r["channel"] for r in recipients} == {"email", "whatsapp", "sms"}


def test_the_export_carries_every_sms_field(sms_client):
    _seed(sms_client)
    data = sms_client.get("/account/export").json()
    sms_row = next(
        r for r in data["events"][0]["recipients"] if r["channel"] == "sms"
    )
    assert sms_row["phone"] == GUEST_SMS
    assert sms_row["sms"]["message_id"] == "SM_abc"
    assert sms_row["sms"]["delivered_at"] is not None
    assert sms_row["sms"]["opted_out"] is True
    assert sms_row["note"] == "can't make it, sorry!"


def test_the_export_records_that_a_contact_opted_out(sms_client):
    """Derived from the opt-out log by the contact's number, and the fact a
    person is most entitled to find in their own export."""
    _seed(sms_client)
    data = sms_client.get("/account/export").json()
    assert data["contacts"][0]["opted_out_sms"] is True
    assert data["contacts"][0]["phone"] == GUEST_SMS


def test_every_exported_timestamp_says_its_zone(sms_client):
    """SQLite hands back naive datetimes. A file a person reads should not have
    to guess: every one ends in +00:00."""
    _seed(sms_client)
    data = sms_client.get("/account/export").json()
    sms_row = next(r for r in data["events"][0]["recipients"] if r["channel"] == "sms")
    for value in (sms_row["sent_at"], sms_row["sms"]["delivered_at"], data["created_at"]):
        assert value is not None and value.endswith("+00:00"), value
    assert sms_row["sms"]["failed_at"] is None


def test_the_export_is_scoped_to_the_signed_in_host(sms_client):
    """Another host's card and address book never appear."""
    _seed(sms_client)
    db = _db()
    other = User(google_sub="someone-else", email="other@example.com", display_name="Other")
    db.add(other)
    db.commit()
    db.add(Event(user_id=other.id, title="Not yours", status="draft"))
    db.add(Contact(user_id=other.id, email="theirs@example.com", email_hash="h-theirs"))
    db.commit()
    data = sms_client.get("/account/export").json()
    assert [e["title"] for e in data["events"]] == ["Joe's 3rd Birthday"]
    assert [c["email"] for c in data["contacts"]] == [""]           # Mara has no email


def test_the_export_carries_no_secrets_or_blind_indexes(sms_client):
    _seed(sms_client)
    dump = json.dumps(sms_client.get("/account/export").json())
    assert "refresh_token" not in dump
    assert "_hash" not in dump
    assert phone_hash(GUEST_SMS) not in dump
    db = _db()
    for row in db.execute(select(Recipient)).scalars():
        assert row.token not in dump, "an invite token is a credential"


def test_the_export_keeps_delivery_facts_apart_from_opened(sms_client):
    """A receipt is not a page visit, in the export as much as in the UI."""
    _seed(sms_client)
    data = sms_client.get("/account/export").json()
    sms_row = next(
        r for r in data["events"][0]["recipients"] if r["channel"] == "sms"
    )
    assert sms_row["sms"]["delivered_at"] is not None
    assert sms_row["first_open_at"] is None
    assert "opened" not in sms_row


def test_the_export_says_whether_the_channel_was_available(sms_client):
    """Whether this host could text, through what, and whose settings did it.

    ``source`` matters to the person reading the file: "the site texted for me"
    and "I texted through my own Twilio" are different facts about their data.
    ``own_setup`` is None here because this host set nothing up — the site did.
    """
    _seed(sms_client)
    data = sms_client.get("/account/export").json()
    assert data["sms"] == {
        "configured": True, "provider": "twilio", "source": "site", "own_setup": None,
    }


def test_the_export_does_not_leak_the_provider_when_the_channel_is_off(off_client):
    data = off_client.get("/account/export").json()
    assert data["sms"]["configured"] is False
    assert data["sms"]["provider"] is None


def test_the_whatsapp_recipient_keeps_its_own_receipt_fields(sms_client):
    """Both channels' facts live side by side, neither folded into the other."""
    _seed(sms_client)
    data = sms_client.get("/account/export").json()
    wa = next(r for r in data["events"][0]["recipients"] if r["channel"] == "whatsapp")
    assert set(wa["whatsapp"]) == {"message_id", "delivered_at", "read_at", "ack"}
    assert wa["sms"]["opted_out"] is False


def test_the_export_is_still_a_download(sms_client):
    _seed(sms_client)
    r = sms_client.get("/account/export")
    assert "attachment" in r.headers["content-disposition"]
    assert "kith-post-export.json" in r.headers["content-disposition"]


def test_an_export_needs_a_session(sms_client):
    sms_client.post("/auth/logout")
    r = sms_client.get("/account/export", follow_redirects=False)
    assert r.status_code == 303


# --- delete --------------------------------------------------------------------

def test_deleting_the_account_removes_the_rows_but_keeps_the_opt_out(sms_client):
    """Everything the host owns goes. The one thing kept is the hashed record
    that a number said STOP — with no user_id and nothing readable, it is the
    only record that has to outlive the account: without it the same number is
    textable again the moment someone signs back in and re-adds it."""
    from kith.services import send as sender

    _seed(sms_client)
    assert sender.opted_out_hashes(_db()) == {phone_hash(GUEST_SMS)}

    assert sms_client.post("/account/delete", follow_redirects=False).status_code == 303

    db2 = _db()
    for model in (User, Event, Recipient, Contact):
        assert db2.execute(select(func.count()).select_from(model)).scalar_one() == 0
    assert db2.execute(select(func.count()).select_from(SmsOptOutEvent)).scalar_one() == 1
    assert sender.opted_out_hashes(db2) == {phone_hash(GUEST_SMS)}


def test_deleting_the_account_removes_the_outbox_too(sms_client):
    """The dry-run outbox holds real addresses and full message texts, and
    dry-run is the default mode, so every default install accumulates them.
    "Delete all your data" has to reach them or it isn't that."""
    ev = _seed(sms_client)
    outbox = get_settings().outbox_dir / ev
    assert list((outbox / "sms").glob("*.txt")), "the seed should have written texts"
    assert list(outbox.glob("*.eml")), "...and an email"

    sms_client.post("/account/delete", follow_redirects=False)
    assert not outbox.exists()


def test_the_cascade_reaches_reminders_for_an_sms_recipient(sms_client):
    """Reminders hang off the recipient, and the SMS batch schedules them itself
    once the texts have gone — so the seed's dry-run send has already planned
    some for the SMS guest."""
    from kith.db.models import Reminder

    ev = _seed(sms_client)
    db = _db()
    sms_row = next(
        r for r in db.execute(
            select(Recipient).where(Recipient.event_id == ev)
        ).scalars() if r.phone == GUEST_SMS
    )
    planned = db.execute(
        select(func.count()).select_from(Reminder).where(Reminder.recipient_id == sms_row.id)
    ).scalar_one()
    assert planned >= 1

    sms_client.post("/account/delete", follow_redirects=False)
    assert _db().execute(select(func.count()).select_from(Reminder)).scalar_one() == 0
