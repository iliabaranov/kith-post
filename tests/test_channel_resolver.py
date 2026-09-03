"""``channel_of`` — the one place that decides which channel a row goes out on.

Separate from test_channels.py, which is about parsing mixed recipient lists and
identity. This is narrower: given a stored row, which channel is it on? The rule
under test is that the ``channel`` column is authoritative and the old
phone-implies-WhatsApp inference survives only for NULL rows, so that a second
phone-based channel can exist without silently stealing WhatsApp's recipients.
"""

from dataclasses import dataclass

from sqlalchemy import select

from kith.core.channels import (
    ALL_CHANNELS,
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    CHANNEL_WHATSAPP,
    channel_of,
)
from kith.core.recipients import CHANNEL_EMAIL as REEXPORTED_EMAIL
from kith.core.recipients import CHANNEL_WHATSAPP as REEXPORTED_WHATSAPP
from kith.db.models import Event, Recipient, User


@dataclass
class Row:
    """The two attributes the resolver reads, and nothing else.

    A stand-in rather than a Recipient so these stay pure unit tests with no
    engine, no session and no schema behind them.
    """

    channel: str | None = None
    phone: str | None = None


# --- the channel column is authoritative -------------------------------------

def test_explicit_channel_is_returned_verbatim():
    assert channel_of(Row(channel="email")) == CHANNEL_EMAIL
    assert channel_of(Row(channel="whatsapp")) == CHANNEL_WHATSAPP
    assert channel_of(Row(channel="sms")) == CHANNEL_SMS


def test_explicit_channel_beats_a_stray_phone():
    """An email row that happens to carry a number is still an email row.

    This is the case the old inference got wrong, and the reason the column has
    to win: a contact with both an address and a mobile must not be texted
    because the number is there.
    """
    assert channel_of(Row(channel="email", phone="+15550001111")) == CHANNEL_EMAIL


def test_sms_row_with_a_phone_stays_sms_not_whatsapp():
    assert channel_of(Row(channel="sms", phone="+15550001111")) == CHANNEL_SMS


# --- NULL predates the column ------------------------------------------------

def test_legacy_row_with_no_phone_is_email():
    assert channel_of(Row(channel=None, phone=None)) == CHANNEL_EMAIL


def test_legacy_row_with_a_phone_is_whatsapp():
    """Rows written before the column existed only ever had one phone channel."""
    assert channel_of(Row(channel=None, phone="+15550001111")) == CHANNEL_WHATSAPP


def test_empty_string_channel_falls_back_like_null():
    """SQLite hands back "" for a column some other writer left blank."""
    assert channel_of(Row(channel="", phone="+15550001111")) == CHANNEL_WHATSAPP
    assert channel_of(Row(channel="", phone=None)) == CHANNEL_EMAIL


# --- shape -------------------------------------------------------------------

def test_a_real_recipient_resolves_the_same_way():
    """The resolver takes ``object``; prove that includes the actual DB model."""
    assert channel_of(Recipient(channel="whatsapp", phone="+15550001111")) == CHANNEL_WHATSAPP
    assert channel_of(Recipient(channel=None, phone=None)) == CHANNEL_EMAIL


def test_missing_attributes_do_not_raise():
    """Anything row-like is fair game, including something carrying neither."""
    assert channel_of(object()) == CHANNEL_EMAIL


def test_all_channels_lists_exactly_the_three():
    assert ALL_CHANNELS == (CHANNEL_EMAIL, CHANNEL_WHATSAPP, CHANNEL_SMS)


def test_constants_stay_importable_from_recipients():
    """tests/test_channels.py and the web layer both import them from there."""
    assert REEXPORTED_EMAIL == CHANNEL_EMAIL
    assert REEXPORTED_WHATSAPP == CHANNEL_WHATSAPP


# --- the send path partitions by the resolver, not by the number -------------

def _db_and_user():
    from kith.config import get_settings
    from kith.db.session import make_engine, make_session_factory

    db = make_session_factory(make_engine(get_settings().db_path))()
    return db, db.execute(select(User)).scalars().first()


def test_dry_run_partitions_a_mixed_event_by_channel(client):
    """An email row carrying a stray number goes out by email, not WhatsApp.

    Built by writing the rows directly, because the compose form can't produce
    this shape — it is the legacy/hand-edited row the old ``if not r.phone``
    split would have misrouted, and the reason the column has to be
    authoritative. The two ordinary rows either side prove the normal case is
    unchanged: one .eml each for the email recipients, one whatsapp/*.txt for
    the WhatsApp one.
    """
    from kith.config import get_settings
    from kith.services import send as sender

    client.post("/auth/dev-login")
    loc = client.post(
        "/events",
        data={"title": "Sunday lunch", "event_date": "2099-06-14",
              "recipients": "mara@example.com", "block_date": "on"},
        follow_redirects=False,
    ).headers["location"]
    event_id = loc.split("/events/")[1].split("?")[0]

    db, user = _db_and_user()
    db.add_all([
        Recipient(
            id="r-wa", event_id=event_id, email="", name="Sam",
            channel=CHANNEL_WHATSAPP, phone="+15552220000",
            token="tok-wa", status="queued",
        ),
        # The awkward one: an email recipient whose contact card also held a
        # mobile. Old logic saw the number and sent it over WhatsApp.
        Recipient(
            id="r-both", event_id=event_id, email="jo@example.com", name="Jo",
            channel=CHANNEL_EMAIL, phone="+15553330000",
            token="tok-both", status="queued",
        ),
    ])
    db.commit()

    ev = db.get(Event, event_id)
    res = sender.send_event(db, ev, user, get_settings())

    assert (res.sent, res.failed) == (3, 0)
    assert (res.wa_sent, res.wa_failed) == (1, 0)

    outbox = get_settings().outbox_dir / event_id
    emls = {f.stem for f in outbox.glob("*.eml")}
    assert len(emls) == 2                    # mara, plus the stray-phone row
    assert "r-both" in emls and "r-wa" not in emls
    assert [f.stem for f in (outbox / "whatsapp").glob("*.txt")] == ["r-wa"]


# --- the resume sweep owes a WhatsApp batch only to WhatsApp rows -------------

def test_resume_owes_a_batch_only_to_whatsapp_rows(client, monkeypatch):
    """A queued number on another channel must not keep resubmitting a WhatsApp batch.

    ``resume_interrupted_wa_batches`` used to count "owed" rows by
    ``phone IS NOT NULL``. A row whose channel column says otherwise would make
    it submit a batch that finds nothing to do and returns *without* clearing
    ``wa_batch_started_at`` — so the sweep would resubmit it on every tick for a
    day. Counting through the resolver clears the marker instead.
    """
    from datetime import UTC, datetime, timedelta

    from kith.config import Settings, get_settings
    from kith.services import scheduler, send

    client.post("/auth/dev-login")
    loc = client.post(
        "/events",
        data={"title": "Sunday lunch", "event_date": "2099-06-14",
              "recipients": "mara@example.com", "block_date": "on"},
        follow_redirects=False,
    ).headers["location"]
    event_id = loc.split("/events/")[1].split("?")[0]

    submitted: list[str] = []
    monkeypatch.setattr(send, "submit_whatsapp_batch", lambda sf, eid, st: submitted.append(eid))
    settings = Settings(
        whatsapp_enabled=True, waha_api_key="k", data_dir=get_settings().data_dir,
    )
    now = datetime.now(UTC)

    db, _ = _db_and_user()
    ev = db.get(Event, event_id)
    ev.wa_batch_started_at = now - timedelta(minutes=10)
    db.add(Recipient(
        id="r-other", event_id=event_id, email="", name="Sam",
        channel="sms", phone="+15552220000", token="tok-other", status="queued",
    ))
    db.commit()

    # A phone number on a non-WhatsApp channel: nothing is owed, marker cleared.
    assert scheduler.resume_interrupted_wa_batches(db, None, settings, now=now) == 0
    assert submitted == []
    assert db.get(Event, event_id).wa_batch_started_at is None

    # A genuine WhatsApp row: resumed exactly once.
    ev = db.get(Event, event_id)
    ev.wa_batch_started_at = now - timedelta(minutes=10)
    db.add(Recipient(
        id="r-wa", event_id=event_id, email="", name="Kim",
        channel=CHANNEL_WHATSAPP, phone="+15553330000", token="tok-wa", status="queued",
    ))
    db.commit()
    assert scheduler.resume_interrupted_wa_batches(db, None, settings, now=now) == 1
    assert submitted == [event_id]
