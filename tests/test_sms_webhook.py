"""SMS delivery receipts and STOP opt-outs.

The compliance-critical module. Three properties are load-bearing:

* **Two providers, two signature schemes, never crossed.** Twilio signs a
  base64 HMAC-SHA1 over the URL plus sorted parameters; the gateway signs a hex
  HMAC-SHA256 over the body plus a timestamp. Verifying either with the other's
  scheme would reject every legitimate request, so each endpoint is tested to
  accept its own and refuse the other's.
* **A receipt is never an "Opened".** Delivered is the carrier's fact about the
  message; opened means a person loaded the invitation page. They stay in
  separate columns.
* **STOP is permanent and enforced everywhere.** Not just on the card the
  person was on, and not just on first sends.

Nothing here touches the network. Signatures are constructed in the test the
way each provider documents.
"""

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kith.config import Settings, get_settings
from kith.db.models import Contact, Event, Recipient, User
from kith.services import sms
from kith.services import sms_twilio as twilio

SECRET = "webhook-signing-secret"
TWILIO_SID, TWILIO_TOKEN = "AC0123456789", "twilio-auth-token"
GUEST = "+15551110000"


# --- fixtures ------------------------------------------------------------------

def _env(**extra):
    base = {
        "KITH_SMS_ENABLED": "true",
        "KITH_SMS_PROVIDER": "twilio",
        "KITH_SMS_TWILIO_ACCOUNT_SID": TWILIO_SID,
        "KITH_SMS_TWILIO_AUTH_TOKEN": TWILIO_TOKEN,
        "KITH_SMS_TWILIO_FROM": "+15550001234",
        "KITH_SMS_WEBHOOK_SECRET": SECRET,
    }
    base.update(extra)
    return base


def _client(monkeypatch, **extra):
    for k, v in _env(**extra).items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    from kith.web.app import create_app

    c = TestClient(create_app())
    c.__enter__()
    c.post("/auth/dev-login")
    return c


@pytest.fixture
def hooked(monkeypatch):
    c = _client(monkeypatch)
    try:
        yield c
    finally:
        c.__exit__(None, None, None)
        get_settings.cache_clear()


def _db_and_user():
    from kith.db.session import make_engine, make_session_factory

    db = make_session_factory(make_engine(get_settings().db_path))()
    return db, db.execute(select(User)).scalars().first()


def _event_with_sms(client, *, to=GUEST, message_id="SM_sent"):
    """A card already sent by text, so a receipt has something to land on."""
    r = client.post(
        "/events",
        data={"title": "Joe's 3rd Birthday", "event_date": "2099-06-14",
              "recipients": "", "wa_recipients": "", "sms_recipients": to,
              "block_rsvp": "on", "block_date": "on"},
        follow_redirects=False,
    )
    ev = r.headers["location"].split("/events/")[1].split("?")[0]
    db, _ = _db_and_user()
    row = db.execute(
        select(Recipient).where(Recipient.event_id == ev)
    ).scalars().first()
    row.status, row.sms_message_id = "sent", message_id
    db.commit()
    return ev, row.id


# --- signing helpers, per each provider's documented scheme -------------------

def _twilio_post(client, params, *, url=None, signature=None):
    url = url or f"{get_settings().base_url}/sms/webhook/twilio"
    if signature is None:
        payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
        signature = base64.b64encode(
            hmac.new(TWILIO_TOKEN.encode(), payload.encode(), hashlib.sha1).digest()
        ).decode()
    return client.post(
        "/sms/webhook/twilio", data=params,
        headers={twilio.TWILIO_SIGNATURE_HEADER: signature},
    )


def _gateway_post(client, body, *, secret=SECRET, ts=None, signature=None, raw=None):
    raw = raw if raw is not None else json.dumps(body).encode()
    ts = str(int(time.time())) if ts is None else str(ts)
    if signature is None:
        signature = hmac.new(secret.encode(), raw + ts.encode(), hashlib.sha256).hexdigest()
    return client.post(
        "/sms/webhook/gateway", content=raw,
        headers={
            "content-type": "application/json",
            sms.GATEWAY_SIGNATURE_HEADER: signature,
            sms.GATEWAY_TIMESTAMP_HEADER: ts,
        },
    )


# --- the gate ------------------------------------------------------------------

def test_both_endpoints_404_when_no_secret_is_set(monkeypatch):
    c = _client(monkeypatch, KITH_SMS_WEBHOOK_SECRET="")
    try:
        assert c.post("/sms/webhook/twilio", data={}).status_code == 404
        assert c.post("/sms/webhook/gateway", json={}).status_code == 404
    finally:
        c.__exit__(None, None, None)
        get_settings.cache_clear()


def test_receipts_are_off_by_default():
    assert Settings(sms_enabled=True).sms_webhooks_configured is False
    assert Settings(sms_enabled=False, sms_webhook_secret="x").sms_webhooks_configured is False
    assert Settings(sms_enabled=True, sms_webhook_secret="x").sms_webhooks_configured is True


def test_the_twilio_callback_url_is_public_and_only_exists_when_on():
    """Unlike the WhatsApp webhook, this call arrives from the internet."""
    off = Settings(sms_enabled=True, base_url="https://kith.example")
    assert off.sms_status_callback_url == ""
    on = Settings(sms_enabled=True, sms_webhook_secret="x", base_url="https://kith.example/")
    assert on.sms_status_callback_url == "https://kith.example/sms/webhook/twilio"


# --- signatures ----------------------------------------------------------------

def test_a_correctly_signed_twilio_callback_is_accepted(hooked):
    _ev, rid = _event_with_sms(hooked)
    r = _twilio_post(hooked, {"MessageSid": "SM_sent", "MessageStatus": "delivered"})
    assert r.status_code == 200
    db, _ = _db_and_user()
    assert db.get(Recipient, rid).sms_delivered_at is not None


def test_a_tampered_twilio_callback_is_refused(hooked):
    _ev, rid = _event_with_sms(hooked)
    params = {"MessageSid": "SM_sent", "MessageStatus": "delivered"}
    url = f"{get_settings().base_url}/sms/webhook/twilio"
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    sig = base64.b64encode(
        hmac.new(TWILIO_TOKEN.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()
    # Same signature, different body: exactly what an attacker would try.
    r = _twilio_post(
        hooked, {"MessageSid": "SM_sent", "MessageStatus": "undelivered"}, signature=sig
    )
    assert r.status_code == 401
    db, _ = _db_and_user()
    assert db.get(Recipient, rid).sms_delivered_at is None


def test_an_unsigned_twilio_callback_is_refused(hooked):
    _event_with_sms(hooked)
    assert hooked.post(
        "/sms/webhook/twilio", data={"MessageSid": "SM_sent", "MessageStatus": "delivered"}
    ).status_code == 401


def test_a_twilio_signature_over_the_wrong_url_is_refused(hooked):
    """The URL is signed material, so a callback aimed elsewhere is not ours."""
    _event_with_sms(hooked)
    r = _twilio_post(
        hooked, {"MessageSid": "SM_sent", "MessageStatus": "delivered"},
        url="https://attacker.example/sms/webhook/twilio",
    )
    assert r.status_code == 401


def test_a_correctly_signed_gateway_callback_is_accepted(hooked):
    _ev, rid = _event_with_sms(hooked, message_id="msg-1")
    r = _gateway_post(hooked, {
        "event": "sms:delivered", "payload": {"messageId": "msg-1"},
    })
    assert r.status_code == 200
    db, _ = _db_and_user()
    assert db.get(Recipient, rid).sms_delivered_at is not None


def test_a_gateway_callback_signed_with_the_wrong_secret_is_refused(hooked):
    _event_with_sms(hooked, message_id="msg-1")
    r = _gateway_post(
        hooked, {"event": "sms:delivered", "payload": {"messageId": "msg-1"}},
        secret="not-the-secret",
    )
    assert r.status_code == 401


def test_a_gateway_body_tampered_after_signing_is_refused(hooked):
    _ev, rid = _event_with_sms(hooked, message_id="msg-1")
    body = {"event": "sms:delivered", "payload": {"messageId": "msg-1"}}
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), raw + ts.encode(), hashlib.sha256).hexdigest()
    r = _gateway_post(
        hooked, body, ts=ts, signature=sig,
        raw=json.dumps({"event": "sms:delivered",
                        "payload": {"messageId": "msg-OTHER"}}).encode(),
    )
    assert r.status_code == 401
    db, _ = _db_and_user()
    assert db.get(Recipient, rid).sms_delivered_at is None


def test_a_stale_gateway_callback_is_refused(hooked):
    """The HMAC stops forgery but not replay: without a freshness check one
    captured "delivered" POST could be re-sent forever."""
    _event_with_sms(hooked, message_id="msg-1")
    r = _gateway_post(
        hooked, {"event": "sms:delivered", "payload": {"messageId": "msg-1"}},
        ts=int(time.time()) - 3600,
    )
    assert r.status_code == 401


def test_a_gateway_callback_from_the_future_is_refused(hooked):
    _event_with_sms(hooked, message_id="msg-1")
    r = _gateway_post(
        hooked, {"event": "sms:delivered", "payload": {"messageId": "msg-1"}},
        ts=int(time.time()) + 3600,
    )
    assert r.status_code == 401


def test_neither_scheme_is_accepted_at_the_others_endpoint(hooked):
    """The whole reason there are two endpoints."""
    _event_with_sms(hooked, message_id="msg-1")
    body = {"event": "sms:delivered", "payload": {"messageId": "msg-1"}}
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    gateway_sig = hmac.new(SECRET.encode(), raw + ts.encode(), hashlib.sha256).hexdigest()
    # A gateway-signed body posted to the Twilio endpoint.
    assert hooked.post(
        "/sms/webhook/twilio", content=raw,
        headers={"content-type": "application/json",
                 twilio.TWILIO_SIGNATURE_HEADER: gateway_sig},
    ).status_code == 401
    # ...and a Twilio-signed form posted to the gateway endpoint.
    params = {"MessageSid": "SM_sent", "MessageStatus": "delivered"}
    url = f"{get_settings().base_url}/sms/webhook/twilio"
    twilio_sig = base64.b64encode(hmac.new(
        TWILIO_TOKEN.encode(),
        (url + "".join(f"{k}{params[k]}" for k in sorted(params))).encode(),
        hashlib.sha1,
    ).digest()).decode()
    assert hooked.post(
        "/sms/webhook/gateway", data=params,
        headers={sms.GATEWAY_SIGNATURE_HEADER: twilio_sig,
                 sms.GATEWAY_TIMESTAMP_HEADER: ts},
    ).status_code == 401


def test_an_oversize_body_is_refused_before_it_is_hashed(hooked):
    from kith.web.routes_sms_webhook import MAX_WEBHOOK_BODY

    big = json.dumps({"event": "sms:delivered", "pad": "x" * (MAX_WEBHOOK_BODY + 100)})
    r = hooked.post(
        "/sms/webhook/gateway", content=big.encode(),
        headers={"content-type": "application/json",
                 sms.GATEWAY_SIGNATURE_HEADER: "deadbeef",
                 sms.GATEWAY_TIMESTAMP_HEADER: str(int(time.time()))},
    )
    assert r.status_code == 413


# --- receipts ------------------------------------------------------------------

def test_a_delivery_receipt_never_sets_an_opened_state(hooked):
    """A receipt is the carrier's fact about the message, not a page visit."""
    _ev, rid = _event_with_sms(hooked)
    _twilio_post(hooked, {"MessageSid": "SM_sent", "MessageStatus": "delivered"})
    db, _ = _db_and_user()
    r = db.get(Recipient, rid)
    assert r.sms_delivered_at is not None
    assert r.first_open_at is None
    assert r.status == "sent"          # not "opened"


def test_the_first_confirmation_is_the_timestamp_that_sticks(hooked):
    """Receipts repeat and arrive out of order."""
    _ev, rid = _event_with_sms(hooked)
    _twilio_post(hooked, {"MessageSid": "SM_sent", "MessageStatus": "delivered"})
    db, _ = _db_and_user()
    first = db.get(Recipient, rid).sms_delivered_at
    _twilio_post(hooked, {"MessageSid": "SM_sent", "MessageStatus": "delivered"})
    db2, _ = _db_and_user()
    assert db2.get(Recipient, rid).sms_delivered_at == first


@pytest.mark.parametrize("status", ["queued", "sending", "sent"])
def test_an_in_flight_status_is_not_a_delivery(hooked, status):
    """"sent" only means Twilio handed it off; the carrier hasn't confirmed."""
    _ev, rid = _event_with_sms(hooked)
    assert _twilio_post(
        hooked, {"MessageSid": "SM_sent", "MessageStatus": status}
    ).status_code == 200
    db, _ = _db_and_user()
    assert db.get(Recipient, rid).sms_delivered_at is None


@pytest.mark.parametrize("status", ["undelivered", "failed"])
def test_a_failure_is_logged_and_not_recorded_as_delivered(hooked, status):
    _ev, rid = _event_with_sms(hooked)
    assert _twilio_post(
        hooked, {"MessageSid": "SM_sent", "MessageStatus": status}
    ).status_code == 200
    db, _ = _db_and_user()
    assert db.get(Recipient, rid).sms_delivered_at is None


@pytest.mark.parametrize("event", ["sms:failed", "sms:cancelled"])
def test_a_gateway_failure_is_not_a_delivery(hooked, event):
    _ev, rid = _event_with_sms(hooked, message_id="msg-1")
    assert _gateway_post(
        hooked, {"event": event, "payload": {"messageId": "msg-1"}}
    ).status_code == 200
    db, _ = _db_and_user()
    assert db.get(Recipient, rid).sms_delivered_at is None


def test_a_receipt_for_an_unknown_message_is_a_no_op_not_an_error(hooked):
    """They arrive for messages sent before a reset, or from another instance
    sharing the provider account. Both providers retry on non-200."""
    _event_with_sms(hooked)
    r = _twilio_post(hooked, {"MessageSid": "SM_stranger", "MessageStatus": "delivered"})
    assert r.status_code == 200


def test_an_unrecognised_gateway_event_is_ignored_with_a_200(hooked):
    assert _gateway_post(hooked, {"event": "app:started", "payload": {}}).status_code == 200


def test_a_gateway_body_that_is_not_json_is_refused(hooked):
    raw = b"not json at all"
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), raw + ts.encode(), hashlib.sha256).hexdigest()
    r = hooked.post(
        "/sms/webhook/gateway", content=raw,
        headers={"content-type": "application/json",
                 sms.GATEWAY_SIGNATURE_HEADER: sig,
                 sms.GATEWAY_TIMESTAMP_HEADER: ts},
    )
    assert r.status_code == 400


# --- the keyword parser --------------------------------------------------------

@pytest.mark.parametrize("body", [
    "STOP", "stop", " Stop ", "STOP.", "stop!", "StopAll", "STOPALL",
    "unsubscribe", "CANCEL", "end", "Quit", "stop all",
])
def test_every_documented_opt_out_keyword_is_recognised(body):
    assert sms.opt_out_intent(body) == "stop"


@pytest.mark.parametrize("body", ["START", "start", "unstop", "Yes", "subscribe"])
def test_the_way_back_in_is_recognised(body):
    assert sms.opt_out_intent(body) == "start"


@pytest.mark.parametrize("body", [
    "stop by any time!", "I'll stop by later", "can't wait", "",
    "please cancel my rsvp", "yes I can come", None,
])
def test_an_ordinary_reply_is_not_an_opt_out(body):
    """A message is only an opt-out if that is all it says. Someone writing
    "stop by any time" is making conversation."""
    assert sms.opt_out_intent(body) is None


# --- STOP ----------------------------------------------------------------------

def test_a_stop_reply_flags_the_recipient_and_the_contact(hooked):
    db, user = _db_and_user()
    from kith.services import contacts as book

    book.add_contact(db, user.id, "", "Mara", phone=GUEST)
    _ev, rid = _event_with_sms(hooked)

    r = _twilio_post(hooked, {"From": GUEST, "To": "+15550001234", "Body": "STOP"})
    assert r.status_code == 200

    db2, _ = _db_and_user()
    assert db2.get(Recipient, rid).opted_out is True
    contact = db2.execute(select(Contact)).scalars().one()
    assert contact.opted_out_sms is True


def test_a_stop_via_the_gateway_works_the_same_way(hooked):
    _ev, rid = _event_with_sms(hooked, message_id="msg-1")
    r = _gateway_post(hooked, {
        "event": "sms:received",
        "payload": {"sender": GUEST, "message": "STOP", "messageId": "in-1"},
    })
    assert r.status_code == 200
    db, _ = _db_and_user()
    assert db.get(Recipient, rid).opted_out is True


def test_start_undoes_a_stop(hooked):
    """A number that opted out by accident otherwise has no route back — the
    host cannot clear it either."""
    _ev, rid = _event_with_sms(hooked)
    _twilio_post(hooked, {"From": GUEST, "To": "+1", "Body": "STOP"})
    _twilio_post(hooked, {"From": GUEST, "To": "+1", "Body": "START"})
    db, _ = _db_and_user()
    assert db.get(Recipient, rid).opted_out is False


def test_an_ordinary_inbound_reply_is_not_stored_anywhere(hooked):
    """A reply to an invitation belongs in the conversation the host is already
    having, not in a database column they never asked for."""
    _ev, rid = _event_with_sms(hooked)
    r = _twilio_post(hooked, {"From": GUEST, "To": "+1", "Body": "sounds lovely, see you then"})
    assert r.status_code == 200
    db, _ = _db_and_user()
    row = db.get(Recipient, rid)
    assert row.opted_out in (False, None)
    assert row.note is None


def test_a_stop_from_an_unparseable_number_is_a_no_op(hooked):
    _ev, rid = _event_with_sms(hooked)
    assert _twilio_post(
        hooked, {"From": "not-a-number", "To": "+1", "Body": "STOP"}
    ).status_code == 200
    db, _ = _db_and_user()
    assert db.get(Recipient, rid).opted_out in (False, None)


# --- enforcement ---------------------------------------------------------------

def _send(event_id):
    from kith.services import send as sender

    db, user = _db_and_user()
    ev = db.get(Event, event_id)
    return db, sender.send_event(db, ev, user, get_settings())


def test_an_opted_out_number_is_not_texted_again(hooked):
    """The point of the whole module. Asserted through a real dry-run send:
    no outbox artifact means no text."""
    ev, rid = _event_with_sms(hooked, to=f"{GUEST}\n+15552220000")
    db, _ = _db_and_user()
    # Put both back in the queue so the send has something to do.
    for r in db.execute(select(Recipient).where(Recipient.event_id == ev)).scalars():
        r.status, r.sms_message_id = "queued", None
    db.commit()

    _twilio_post(hooked, {"From": GUEST, "To": "+1", "Body": "STOP"})
    db2, res = _send(ev)

    assert res.sms_sent == 1              # the other guest, not this one
    written = [f.stem for f in (get_settings().outbox_dir / ev / "sms").glob("*.txt")]
    opted = [
        r for r in db2.execute(
            select(Recipient).where(Recipient.event_id == ev)
        ).scalars() if r.phone == GUEST
    ][0]
    assert opted.id not in written
    assert opted.status == "queued"       # never sent, never falsely failed


def test_a_number_that_opted_out_is_skipped_on_a_brand_new_card(hooked):
    """The gap worth closing: a card composed *after* the STOP has a fresh
    recipient row that knows nothing about it. The number is still opted out."""
    first, _rid = _event_with_sms(hooked)
    _twilio_post(hooked, {"From": GUEST, "To": "+1", "Body": "STOP"})

    second, _rid2 = _event_with_sms(hooked, to=GUEST, message_id="SM_second")
    db, _ = _db_and_user()
    for r in db.execute(select(Recipient).where(Recipient.event_id == second)).scalars():
        r.status, r.sms_message_id = "queued", None
    db.commit()

    _db, res = _send(second)
    assert res.sms_sent == 0
    assert not list((get_settings().outbox_dir / second / "sms").glob("*.txt"))


def test_an_opted_out_recipient_is_not_nudged_either(hooked):
    """Enforced on reminders, not only first sends."""
    from datetime import UTC, datetime, timedelta

    from kith.db.models import Reminder
    from kith.services import scheduler

    ev, rid = _event_with_sms(hooked)
    db, _ = _db_and_user()
    db.add(Reminder(
        event_id=ev, recipient_id=rid, offset_label="manual", status="pending",
        scheduled_for=datetime.now(UTC) - timedelta(hours=1),
    ))
    db.commit()

    _twilio_post(hooked, {"From": GUEST, "To": "+1", "Body": "STOP"})

    db2, _ = _db_and_user()
    reminder = db2.execute(select(Reminder)).scalars().one()
    assert scheduler.send_one_reminder(db2, reminder, get_settings()) is False
    db3, _ = _db_and_user()
    fired = db3.execute(select(Reminder)).scalars().one()
    assert fired.status == "skipped" and fired.skip_reason == "opted_out"
    assert not list((get_settings().outbox_dir / ev / "sms" / "reminders").glob("*.txt"))


def test_an_ordinary_recipient_is_still_nudged_by_text(hooked):
    """The SMS reminder path exists at all — before this it fell through to the
    email branch and tried to mail an address of ""."""
    from datetime import UTC, datetime, timedelta

    from kith.db.models import Reminder
    from kith.services import scheduler

    ev, rid = _event_with_sms(hooked)
    db, _ = _db_and_user()
    db.add(Reminder(
        event_id=ev, recipient_id=rid, offset_label="manual", status="pending",
        scheduled_for=datetime.now(UTC) - timedelta(hours=1),
    ))
    db.commit()

    db2, _ = _db_and_user()
    reminder = db2.execute(select(Reminder)).scalars().one()
    assert scheduler.send_one_reminder(db2, reminder, get_settings()) is True

    files = list((get_settings().outbox_dir / ev / "sms" / "reminders").glob("*.txt"))
    assert len(files) == 1
    body = files[0].read_text()
    assert body.startswith(f"To: {GUEST}\n")
    assert "nudge" in body
    assert ".eml" not in body


def test_the_lookup_only_holds_people_who_asked_to_be_left_alone(hooked):
    from kith.services import send as sender

    ev, _rid = _event_with_sms(hooked)
    db, user = _db_and_user()
    assert sender.opted_out_numbers(db, user.id) == set()

    _twilio_post(hooked, {"From": GUEST, "To": "+1", "Body": "STOP"})
    db2, user2 = _db_and_user()
    assert sender.opted_out_numbers(db2, user2.id) == {GUEST}


# --- the status callback -------------------------------------------------------

def test_twilio_sends_register_a_status_callback_once_receipts_are_on():
    import httpx

    from kith.services.sms_twilio import TwilioProvider

    seen = {}

    def handler(request):
        from urllib.parse import parse_qs

        seen.update(parse_qs(request.content.decode()))
        return httpx.Response(201, json={"sid": "SM1"})

    TwilioProvider(
        TWILIO_SID, TWILIO_TOKEN, from_number="+1",
        status_callback="https://kith.example/sms/webhook/twilio",
        transport=httpx.MockTransport(handler),
    ).send(GUEST, "hi")
    assert seen["StatusCallback"] == ["https://kith.example/sms/webhook/twilio"]


def test_no_callback_is_registered_when_receipts_are_off():
    import httpx

    from kith.services.sms_twilio import TwilioProvider

    seen = {}

    def handler(request):
        from urllib.parse import parse_qs

        seen.update(parse_qs(request.content.decode()))
        return httpx.Response(201, json={"sid": "SM1"})

    TwilioProvider(
        TWILIO_SID, TWILIO_TOKEN, from_number="+1",
        transport=httpx.MockTransport(handler),
    ).send(GUEST, "hi")
    assert "StatusCallback" not in seen


def test_the_factory_threads_the_callback_url_through():
    from kith.services.sms_twilio import TwilioProvider

    s = Settings(
        sms_enabled=True, sms_provider="twilio", sms_twilio_account_sid=TWILIO_SID,
        sms_twilio_auth_token=TWILIO_TOKEN, sms_twilio_from="+1",
        sms_webhook_secret=SECRET, base_url="https://kith.example",
    )
    p = sms.get_provider(s)
    assert isinstance(p, TwilioProvider)
    assert p._status_callback == "https://kith.example/sms/webhook/twilio"
