"""Sending invitations over SMS, and the provider seam behind them.

Everything provable in this phase is provable in dry-run: there is no concrete
provider yet, so a live send goes to NullProvider and raises on purpose. What
matters here is that the channel is real end to end — the right text reaches the
outbox, addressed to the right number, and the queue moves — and that the three
channels stay in their own lanes on a mixed card.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kith.config import get_settings
from kith.core.channels import CHANNEL_EMAIL, CHANNEL_SMS, CHANNEL_WHATSAPP
from kith.core.recipients import parse_phones, parse_sms
from kith.db.models import Event, Recipient, User
from kith.services import send as sender
from kith.services import sms


@pytest.fixture
def sms_client(monkeypatch):
    """A signed-in client with the SMS channel configured.

    "twilio" plus dummy credentials is the cheapest way to make sms_configured
    true; nothing in this module sends, so no credential is ever used. Settings
    are cached, so switching the channel on means a fresh app.
    """
    monkeypatch.setenv("KITH_SMS_ENABLED", "true")
    monkeypatch.setenv("KITH_SMS_PROVIDER", "twilio")
    get_settings.cache_clear()
    try:
        from kith.web.app import create_app

        with TestClient(create_app()) as c:
            c.post("/auth/dev-login")
            db, user = _db_and_user()
            user.display_name = "Ilia"
            db.commit()
            yield c
    finally:
        get_settings.cache_clear()


def _db_and_user():
    from kith.db.session import make_engine, make_session_factory

    db = make_session_factory(make_engine(get_settings().db_path))()
    return db, db.execute(select(User)).scalars().first()


def _make_event(client, *, sms_to="", emails="", wa_to="", title="Joe's 3rd Birthday"):
    r = client.post(
        "/events",
        data={"title": title, "event_date": "2099-06-14", "event_time": "15:00",
              "recipients": emails, "wa_recipients": wa_to, "sms_recipients": sms_to,
              "block_rsvp": "on", "block_date": "on", "block_time": "on"},
        follow_redirects=False,
    )
    return r.headers["location"].split("/events/")[1].split("?")[0]


def _send(event_id):
    db, user = _db_and_user()
    ev = db.get(Event, event_id)
    return db, sender.send_event(db, ev, user, get_settings())


def _recipients(db, event_id):
    return db.execute(
        select(Recipient).where(Recipient.event_id == event_id)
    ).scalars().all()


def _sms_outbox(event_id):
    return sorted((get_settings().outbox_dir / event_id / "sms").glob("*.txt"))


# --- parsing ------------------------------------------------------------------

def test_parse_sms_tags_the_sms_channel_and_parse_phones_still_says_whatsapp():
    """Same numbers, same validation; only the tag differs."""
    sms_valid, _ = parse_sms("Mara <+15551110000>")
    wa_valid, _ = parse_phones("Mara <+15551110000>")
    assert [p.channel for p in sms_valid] == [CHANNEL_SMS]
    assert [p.channel for p in wa_valid] == [CHANNEL_WHATSAPP]
    assert sms_valid[0].phone == wa_valid[0].phone == "+15551110000"
    assert sms_valid[0].identity == wa_valid[0].identity == "tel:+15551110000"


def test_parse_sms_rejects_a_number_with_no_country_code():
    """Guessing a country is how you text a stranger on the other side of it."""
    valid, invalid = parse_sms("5551110000")
    assert valid == [] and invalid == ["5551110000"]


# --- persistence --------------------------------------------------------------

def test_the_sms_box_creates_sms_recipients(sms_client):
    ev = _make_event(sms_client, sms_to="Mara <+15551110000>\n+15552220000")
    db, _ = _db_and_user()
    rows = _recipients(db, ev)
    assert len(rows) == 2
    assert {r.channel for r in rows} == {CHANNEL_SMS}
    assert sorted(r.phone for r in rows) == ["+15551110000", "+15552220000"]
    assert all(r.email == "" and r.status == "queued" and r.token for r in rows)


def test_the_three_boxes_land_on_three_channels(sms_client):
    ev = _make_event(
        sms_client, emails="mara@example.com", wa_to="+15551110000", sms_to="+15552220000",
    )
    db, _ = _db_and_user()
    by_channel = {r.channel: r for r in _recipients(db, ev)}
    assert set(by_channel) == {CHANNEL_EMAIL, CHANNEL_WHATSAPP, CHANNEL_SMS}
    assert by_channel[CHANNEL_WHATSAPP].phone == "+15551110000"
    assert by_channel[CHANNEL_SMS].phone == "+15552220000"


def test_moving_a_number_from_whatsapp_to_sms_moves_the_channel(sms_client):
    """Both phone channels give the same person the same identity.

    Keying reconciliation on identity alone would match the WhatsApp row against
    the SMS entry and "keep" it, leaving the recipient on WhatsApp while the form
    says otherwise — an edit the host cannot then undo.
    """
    ev = _make_event(sms_client, wa_to="+15551110000")
    db, _ = _db_and_user()
    assert [r.channel for r in _recipients(db, ev)] == [CHANNEL_WHATSAPP]

    sms_client.post(
        f"/events/{ev}",
        data={"title": "Joe's 3rd Birthday", "event_date": "2099-06-14",
              "event_time": "15:00", "recipients": "", "wa_recipients": "",
              "sms_recipients": "+15551110000",
              "block_rsvp": "on", "block_date": "on", "block_time": "on"},
        follow_redirects=False,
    )
    db2, _ = _db_and_user()
    rows = _recipients(db2, ev)
    assert [r.channel for r in rows] == [CHANNEL_SMS]
    assert [r.phone for r in rows] == ["+15551110000"]


def test_a_number_in_both_phone_boxes_becomes_one_recipient(sms_client):
    """One person, one channel — and one invitation, not two."""
    ev = _make_event(sms_client, wa_to="+15551110000", sms_to="+15551110000")
    db, _ = _db_and_user()
    rows = _recipients(db, ev)
    assert len(rows) == 1
    assert rows[0].channel == CHANNEL_WHATSAPP   # the first box wins


# --- dry-run send -------------------------------------------------------------

def test_dry_run_writes_the_text_addressed_to_the_number(sms_client):
    ev = _make_event(sms_client, sms_to="Mara <+15551110000>")
    db, res = _send(ev)
    assert (res.sms_sent, res.sms_failed, res.sms_blocked) == (1, 0, None)
    assert res.sent == 1

    files = _sms_outbox(ev)
    assert len(files) == 1
    body = files[0].read_text()
    assert body.startswith("To: +15551110000\n")
    assert "Segments: 1" in body
    assert "Joe's 3rd Birthday" in body
    assert "Hi Mara - it's Ilia." in body
    token = _recipients(db, ev)[0].token
    assert f"/i/{token}" in body


def test_dry_run_flips_the_queue_to_sent(sms_client):
    ev = _make_event(sms_client, sms_to="+15551110000\n+15552220000")
    db, res = _send(ev)
    assert res.sms_sent == 2
    rows = _recipients(db, ev)
    assert all(r.status == "sent" and r.sent_at for r in rows)
    # No provider was called, so there is no provider id to store.
    assert all(r.sms_message_id is None for r in rows)


def test_the_outbox_file_is_named_for_the_recipient(sms_client):
    ev = _make_event(sms_client, sms_to="+15551110000")
    db, _ = _send(ev)
    assert [f.stem for f in _sms_outbox(ev)] == [_recipients(db, ev)[0].id]


def test_a_mixed_card_produces_all_three_artifact_kinds(sms_client):
    ev = _make_event(
        sms_client, emails="mara@example.com", wa_to="+15551110000", sms_to="+15552220000",
    )
    db, res = _send(ev)
    assert (res.sent, res.failed) == (3, 0)
    assert (res.wa_sent, res.sms_sent) == (1, 1)
    assert (res.wa_blocked, res.sms_blocked) == (None, None)

    outbox = get_settings().outbox_dir / ev
    assert len(list(outbox.glob("*.eml"))) == 1
    assert len(list((outbox / "whatsapp").glob("*.txt"))) == 1
    assert len(list((outbox / "sms").glob("*.txt"))) == 1
    assert all(r.status == "sent" for r in _recipients(db, ev))


def test_an_sms_send_carries_no_card_image(sms_client):
    """SMS is text. MMS is a different product at a different price."""
    ev = _make_event(sms_client, sms_to="+15551110000")
    _send(ev)
    body = _sms_outbox(ev)[0].read_text()
    assert "Card:" not in body          # the WhatsApp outbox line has no analogue
    assert ".jpg" not in body


def test_the_message_carries_the_invitation_link_and_no_tracking(sms_client):
    ev = _make_event(sms_client, sms_to="+15551110000")
    db, _ = _send(ev)
    token = _recipients(db, ev)[0].token
    body = _sms_outbox(ev)[0].read_text()
    assert body.count(f"{get_settings().base_url}/i/{token}") == 1
    assert "utm_" not in body and "?" not in body.split("/i/")[-1]


def test_a_recipient_removed_before_the_send_is_not_texted(sms_client):
    """The batch re-reads each row: a dry-run proves the guard without pacing."""
    ev = _make_event(sms_client, sms_to="+15551110000\n+15552220000")
    db, _ = _db_and_user()
    rows = _recipients(db, ev)
    doomed = rows[0].id
    db.delete(rows[0])
    db.commit()
    _, res = _send(ev)
    assert res.sms_sent == 1
    assert doomed not in [f.stem for f in _sms_outbox(ev)]


# --- the channel stays off unless configured ----------------------------------

def test_the_channel_is_off_by_default():
    s = get_settings()
    assert s.sms_enabled is False
    assert s.sms_configured is False
    assert s.sms_provider == "none"


def test_enabled_without_a_provider_is_still_not_configured(monkeypatch):
    """"none" is a real setting, not a placeholder to be optimistic about."""
    monkeypatch.setenv("KITH_SMS_ENABLED", "true")
    get_settings.cache_clear()
    try:
        assert get_settings().sms_configured is False
    finally:
        get_settings.cache_clear()


# --- the provider seam --------------------------------------------------------

def test_the_factory_returns_the_null_provider_while_none_exist(sms_client):
    provider = sms.get_provider(get_settings())
    assert isinstance(provider, sms.NullProvider)
    assert provider.capabilities() == sms.SmsCaps(can_receipt=False, can_inbound=False)


def test_the_null_provider_raises_rather_than_silently_dropping():
    """A no-op that reported success would flip recipients to 'sent' and tell
    the host their invitations went out when nothing left the building."""
    with pytest.raises(sms.SmsNotConfigured):
        sms.NullProvider().send("+15551110000", "hi")


def test_the_error_hierarchy_lets_one_except_catch_them_all():
    for exc in (sms.SmsAuthError, sms.SmsTimeout, sms.SmsNotConfigured):
        assert issubclass(exc, sms.SmsError)


def test_a_live_send_with_no_provider_stops_the_batch_and_keeps_them_queued(
    sms_client, monkeypatch
):
    """The recipients are still owed a text, so they stay owed."""
    ev = _make_event(sms_client, sms_to="+15551110000\n+15552220000")
    monkeypatch.setenv("KITH_SEND_MODE", "live")
    get_settings.cache_clear()
    db, res = _send(ev)
    assert (res.sms_sent, res.sms_failed) == (0, 0)
    assert res.sms_blocked == "not-configured"
    assert all(r.status == "queued" for r in _recipients(db, ev))
    assert not (get_settings().outbox_dir / ev / "sms").exists()


def test_self_only_writes_the_outbox_rather_than_texting_the_guest(
    sms_client, monkeypatch
):
    """There is no host number setting yet. Until there is, self-only must not
    fall through to the real recipient — that is the one thing it exists for."""
    ev = _make_event(sms_client, sms_to="+15551110000")
    monkeypatch.setenv("KITH_SEND_MODE", "self-only")
    get_settings.cache_clear()
    db, res = _send(ev)
    assert res.sms_sent == 1
    assert len(_sms_outbox(ev)) == 1
    assert all(r.status == "sent" for r in _recipients(db, ev))


# --- pacing -------------------------------------------------------------------

def test_sms_paces_faster_than_whatsapp(monkeypatch):
    """A carrier throttles where WhatsApp bans, so the gap is shorter — but it
    is still a random gap, because an even cadence is its own tell."""
    monkeypatch.setenv("KITH_SMS_SEND_GAP_MIN_SECONDS", "1")
    monkeypatch.setenv("KITH_SMS_SEND_GAP_MAX_SECONDS", "4")
    get_settings.cache_clear()
    try:
        s = get_settings()
        gaps = {sender.next_send_gap(s, channel=CHANNEL_SMS) for _ in range(50)}
        assert all(1.0 <= g <= 4.0 for g in gaps)
        assert len(gaps) > 1                     # actually random, not a constant
        wa = {sender.next_send_gap(s, channel=CHANNEL_WHATSAPP) for _ in range(50)}
        assert all(5.0 <= g <= 20.0 for g in wa)
        # The default is WhatsApp, so existing callers are unaffected.
        assert all(5.0 <= sender.next_send_gap(s) <= 20.0 for _ in range(10))
    finally:
        get_settings.cache_clear()


def test_the_single_flight_guard_is_per_channel(sms_client):
    """An event's two phone halves walk disjoint recipients, so one must not
    lock the other out — but neither may run twice."""
    ev = _make_event(sms_client, wa_to="+15551110000", sms_to="+15552220000")
    assert sender.sms_batch_running(ev) is False
    assert sender.wa_batch_running(ev) is False
    with sender._claim(ev, CHANNEL_SMS) as claimed:
        assert claimed
        assert sender.sms_batch_running(ev) is True
        assert sender.wa_batch_running(ev) is False      # not locked out
        with sender._claim(ev, CHANNEL_SMS) as again:
            assert again is False                        # nor run twice
    assert sender.sms_batch_running(ev) is False
