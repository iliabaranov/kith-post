"""Receipts pushed back from WAHA.

The signature check is the whole security model here — it's the only endpoint a
machine talks to — and the separation between a *receipt* and an *open* is the
whole design point, so both get pinned down.
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kith.config import Settings, get_settings
from kith.db.models import Recipient, User
from kith.services import waha

SECRET = "webhook-test-secret"


def _db_and_user():
    from kith.db.session import make_engine, make_session_factory

    db = make_session_factory(make_engine(get_settings().db_path))()
    return db, db.execute(select(User)).scalars().first()


def _signed(client, body: dict, secret: str = SECRET, header: str | None = None):
    raw = json.dumps(body).encode()
    sig = header if header is not None else hmac.new(
        secret.encode(), raw, hashlib.sha512
    ).hexdigest()
    return client.post(
        "/wa/webhook", content=raw,
        headers={"content-type": "application/json", "X-Webhook-Hmac": sig},
    )


@pytest.fixture
def wa(monkeypatch):
    monkeypatch.setenv("KITH_WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("KITH_WAHA_API_KEY", "test-key")
    monkeypatch.setenv("KITH_WAHA_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("KITH_SEND_MODE", "live")
    get_settings.cache_clear()
    try:
        from kith.web.app import create_app

        with TestClient(create_app()) as c:
            c.post("/auth/dev-login")
            db, user = _db_and_user()
            user.wa_session, user.wa_status = "utest", waha.STATUS_WORKING
            user.wa_number, user.display_name = "+15550009999", "Ilia"
            user.refresh_token = "tok"
            db.commit()
            yield c
    finally:
        get_settings.cache_clear()


def _recipient_with_message(client, message_id="false_15551110000@c.us_AAA"):
    r = client.post(
        "/events",
        data={"title": "Party", "wa_recipients": "Mara <+15551110000>",
              "block_rsvp": "on"},
        follow_redirects=False,
    )
    ev = r.headers["location"].split("/events/")[1].split("?")[0]
    db, _ = _db_and_user()
    rec = db.execute(
        select(Recipient).where(Recipient.event_id == ev)
    ).scalars().first()
    rec.wa_message_id, rec.status = message_id, "sent"
    db.commit()
    return db, ev, rec


# --- the signature is the security model --------------------------------------

def test_an_unsigned_post_is_refused(wa):
    r = wa.post("/wa/webhook", json={"event": "session.status"})
    assert r.status_code == 401


def test_a_wrongly_signed_post_is_refused(wa):
    r = _signed(wa, {"event": "session.status"}, secret="not-the-secret")
    assert r.status_code == 401


def test_a_tampered_body_is_refused(wa):
    """Sign one body, send another — the signature covers the exact bytes."""
    raw = json.dumps({"event": "session.status", "session": "utest",
                      "payload": {"status": "WORKING"}}).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha512).hexdigest()
    r = wa.post(
        "/wa/webhook",
        content=json.dumps({"event": "session.status", "session": "utest",
                            "payload": {"status": "FAILED"}}).encode(),
        headers={"content-type": "application/json", "X-Webhook-Hmac": sig},
    )
    assert r.status_code == 401


def test_the_signature_matches_wahas_real_scheme():
    """Verified against a live 2026.8.1 container: hex HMAC-SHA512 of the raw body."""
    raw = b'{"event":"session.status"}'
    sig = hmac.new(b"k", raw, hashlib.sha512).hexdigest()
    assert waha.verify_webhook("k", raw, sig)
    assert waha.verify_webhook("k", raw, sig.upper())      # case-insensitive hex
    assert not waha.verify_webhook("k", raw + b" ", sig)
    assert not waha.verify_webhook("", raw, sig)           # no secret, no trust
    assert not waha.verify_webhook("k", raw, None)


def test_the_endpoint_is_absent_when_receipts_are_off(client):
    """No secret means no webhook is configured and nothing is accepted."""
    r = client.post("/wa/webhook", json={"event": "session.status"})
    assert r.status_code == 404


# --- session status -----------------------------------------------------------

def test_a_status_event_updates_the_cached_status(wa):
    r = _signed(wa, {"event": "session.status", "session": "utest",
                     "payload": {"status": "FAILED"}})
    assert r.status_code == 200
    db, user = _db_and_user()
    assert user.wa_status == "FAILED"


def test_a_status_event_for_someone_elses_session_is_ignored(wa):
    r = _signed(wa, {"event": "session.status", "session": "usomeoneelse",
                     "payload": {"status": "FAILED"}})
    assert r.status_code == 200
    db, user = _db_and_user()
    assert user.wa_status == waha.STATUS_WORKING


def test_a_dropped_session_reaches_the_dashboard_banner(wa):
    """The point of listening at all: a link that dies between page visits."""
    _signed(wa, {"event": "session.status", "session": "utest",
                 "payload": {"status": "FAILED"}})
    assert "connection has dropped" in wa.get("/").text


# --- delivery and read receipts ----------------------------------------------

def test_delivery_and_read_are_recorded(wa):
    db, ev, rec = _recipient_with_message(wa)
    _signed(wa, {"event": "message.ack", "session": "utest",
                 "payload": {"id": rec.wa_message_id, "ack": waha.ACK_DEVICE,
                             "ackName": "DEVICE"}})
    db2, _ = _db_and_user()
    fresh = db2.get(Recipient, rec.id)
    assert fresh.wa_delivered_at is not None and fresh.wa_read_at is None
    assert fresh.wa_ack == waha.ACK_DEVICE

    _signed(wa, {"event": "message.ack", "session": "utest",
                 "payload": {"id": rec.wa_message_id, "ack": waha.ACK_READ,
                             "ackName": "READ"}})
    db3, _ = _db_and_user()
    fresh = db3.get(Recipient, rec.id)
    assert fresh.wa_read_at is not None and fresh.wa_ack == waha.ACK_READ


def test_a_read_receipt_is_not_an_open(wa):
    """The line this feature must not cross. Opened means a person loaded the
    invitation page; a read receipt is WhatsApp telling the host what it knows."""
    db, ev, rec = _recipient_with_message(wa)
    _signed(wa, {"event": "message.ack", "session": "utest",
                 "payload": {"id": rec.wa_message_id, "ack": waha.ACK_READ,
                             "ackName": "READ"}})
    db2, _ = _db_and_user()
    fresh = db2.get(Recipient, rec.id)
    assert fresh.wa_read_at is not None
    assert fresh.first_open_at is None, "a receipt must never fabricate an open"
    assert fresh.status == "sent"


def test_a_read_receipt_does_not_stop_reminders(wa):
    """...and therefore must not cancel a nudge either."""
    from kith.core import reminders as rem

    db, ev, rec = _recipient_with_message(wa)
    _signed(wa, {"event": "message.ack", "session": "utest",
                 "payload": {"id": rec.wa_message_id, "ack": waha.ACK_READ}})
    db2, _ = _db_and_user()
    fresh = db2.get(Recipient, rec.id)
    assert rem.still_needs_nudge(fresh.status, fresh.first_open_at, "not-clicked")


def test_acks_only_move_forwards(wa):
    """They arrive out of order and repeat."""
    db, ev, rec = _recipient_with_message(wa)
    for ack in (waha.ACK_READ, waha.ACK_SERVER, waha.ACK_DEVICE, waha.ACK_READ):
        _signed(wa, {"event": "message.ack", "session": "utest",
                     "payload": {"id": rec.wa_message_id, "ack": ack}})
    db2, _ = _db_and_user()
    fresh = db2.get(Recipient, rec.id)
    assert fresh.wa_ack == waha.ACK_READ
    first_read = fresh.wa_read_at
    _signed(wa, {"event": "message.ack", "session": "utest",
                 "payload": {"id": rec.wa_message_id, "ack": waha.ACK_READ}})
    db3, _ = _db_and_user()
    assert db3.get(Recipient, rec.id).wa_read_at == first_read  # not re-stamped


def test_a_delivery_failure_is_recorded(wa):
    db, ev, rec = _recipient_with_message(wa)
    _signed(wa, {"event": "message.ack", "session": "utest",
                 "payload": {"id": rec.wa_message_id, "ack": waha.ACK_ERROR,
                             "ackName": "ERROR"}})
    db2, _ = _db_and_user()
    fresh = db2.get(Recipient, rec.id)
    assert fresh.wa_ack == waha.ACK_ERROR
    assert fresh.wa_delivered_at is None


def test_an_ack_for_an_unrelated_chat_is_ignored(wa):
    """The host's own conversations generate acks too."""
    db, ev, rec = _recipient_with_message(wa)
    r = _signed(wa, {"event": "message.ack", "session": "utest",
                     "payload": {"id": "false_9999@c.us_NOTOURS", "ack": 3}})
    assert r.status_code == 200
    db2, _ = _db_and_user()
    assert db2.get(Recipient, rec.id).wa_read_at is None


def test_an_unknown_event_is_accepted_and_ignored(wa):
    """WAHA retries on failure, so an event we don't handle must not 500."""
    r = _signed(wa, {"event": "message.reaction", "session": "utest", "payload": {}})
    assert r.status_code == 200


def test_a_malformed_payload_does_not_crash(wa):
    for body in ({"event": "message.ack", "payload": {}},
                 {"event": "session.status", "payload": {}},
                 {"event": "message.ack", "payload": {"id": "x", "ack": "three"}},
                 {"nonsense": True}):
        assert _signed(wa, body).status_code == 200


# --- what the host sees -------------------------------------------------------

def test_the_dashboard_shows_receipts_as_their_own_line(wa):
    db, ev, rec = _recipient_with_message(wa)
    _signed(wa, {"event": "message.ack", "session": "utest",
                 "payload": {"id": rec.wa_message_id, "ack": waha.ACK_DEVICE}})
    body = wa.get(f"/events/{ev}").text
    assert "Delivered on WhatsApp" in body
    assert "Opened" not in body.split("rsvp-receipt")[0][-400:]

    _signed(wa, {"event": "message.ack", "session": "utest",
                 "payload": {"id": rec.wa_message_id, "ack": waha.ACK_READ}})
    assert "Read on WhatsApp" in wa.get(f"/events/{ev}").text


def test_a_failure_is_spelled_out_to_the_host(wa):
    db, ev, rec = _recipient_with_message(wa)
    _signed(wa, {"event": "message.ack", "session": "utest",
                 "payload": {"id": rec.wa_message_id, "ack": waha.ACK_ERROR}})
    # r.receipt is a template variable, so Jinja escapes the apostrophe.
    assert "deliver it" in wa.get(f"/events/{ev}").text


# --- configuration ------------------------------------------------------------

def test_receipts_need_both_a_url_and_a_secret():
    assert not Settings(whatsapp_enabled=True).waha_webhooks_configured
    assert not Settings(
        whatsapp_enabled=True, waha_webhook_secret=""
    ).waha_webhooks_configured
    assert Settings(
        whatsapp_enabled=True, waha_webhook_secret="s"
    ).waha_webhooks_configured
    # ...and the channel being off disables them regardless.
    assert not Settings(whatsapp_enabled=False,
                        waha_webhook_secret="s").waha_webhooks_configured


def test_a_session_is_created_with_the_webhook_pointed_at_us():
    import httpx

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/sessions":
            seen.update(json.loads(request.content))
            return httpx.Response(201, json={"name": "u1", "status": "STARTING"})
        return httpx.Response(404, json={"message": "Session not found"})

    c = waha.WahaClient(
        "http://waha:3000", "k", 5.0, transport=httpx.MockTransport(handler),
        webhook_url="http://kith:8000/wa/webhook", webhook_secret="s",
    )
    c.ensure_session("u1")
    hooks = seen["config"]["webhooks"]
    assert hooks[0]["url"] == "http://kith:8000/wa/webhook"
    assert set(hooks[0]["events"]) == {"message.ack", "session.status"}
    assert hooks[0]["hmac"]["key"] == "s"


def test_an_existing_session_gets_its_webhooks_pointed_at_us():
    """Sessions linked before receipts existed have none, and WAHA won't guess."""
    import httpx

    puts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"name": "u1", "status": "WORKING",
                                             "config": {"webhooks": []}})
        puts.append(json.loads(request.content))
        return httpx.Response(200, json={"name": "u1", "status": "WORKING"})

    c = waha.WahaClient(
        "http://waha:3000", "k", 5.0, transport=httpx.MockTransport(handler),
        webhook_url="http://kith:8000/wa/webhook", webhook_secret="s",
    )
    assert c.ensure_webhooks("u1") is True
    assert puts[0]["config"]["webhooks"][0]["url"] == "http://kith:8000/wa/webhook"


def test_webhooks_already_pointed_at_us_are_left_alone():
    import httpx

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={
            "name": "u1", "status": "WORKING",
            "config": {"webhooks": [{"url": "http://kith:8000/wa/webhook"}]},
        })

    c = waha.WahaClient(
        "http://waha:3000", "k", 5.0, transport=httpx.MockTransport(handler),
        webhook_url="http://kith:8000/wa/webhook", webhook_secret="s",
    )
    assert c.ensure_webhooks("u1") is False
    assert calls == ["GET"]


def test_no_webhook_is_configured_when_receipts_are_off():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert "config" not in json.loads(request.content)
            return httpx.Response(201, json={"name": "u1", "status": "STARTING"})
        return httpx.Response(404, json={"message": "Session not found"})

    c = waha.WahaClient(
        "http://waha:3000", "k", 5.0, transport=httpx.MockTransport(handler)
    )
    c.ensure_session("u1")
    assert c.ensure_webhooks("u1") is False


# --- hardening from the pre-release security review ---------------------------

def test_an_oversized_body_is_refused_before_it_is_hashed(wa):
    """The endpoint is reachable from the internet by anyone who finds it, so an
    unsigned body must not be buffered and HMAC'd at whatever size they choose."""
    from kith.web.routes_wa_webhook import MAX_WEBHOOK_BODY

    big = b'{"event":"x","pad":"' + b"A" * (MAX_WEBHOOK_BODY + 1000) + b'"}'
    r = wa.post("/wa/webhook", content=big,
                headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_an_unknown_session_status_is_not_stored(wa):
    """The field is an unbounded string from the payload and ends up in the
    dashboard banner; only values WAHA defines are accepted."""
    _signed(wa, {"event": "session.status", "session": "utest",
                 "payload": {"status": "TOTALLY MADE UP <b>x</b>"}})
    db, user = _db_and_user()
    assert user.wa_status == waha.STATUS_WORKING


def test_a_receipt_cannot_be_stamped_onto_another_accounts_recipient(wa):
    """Message ids are unguessable, but a holder of the shared secret still
    shouldn't be able to write a receipt across accounts."""
    db, ev, rec = _recipient_with_message(wa)
    _signed(wa, {"event": "message.ack", "session": "someone-elses-session",
                 "payload": {"id": rec.wa_message_id, "ack": waha.ACK_READ}})
    db2, _ = _db_and_user()
    assert db2.get(Recipient, rec.id).wa_read_at is None


def test_a_delivery_failure_is_recorded_even_after_a_success_ack(wa):
    """ERROR is -1, so a plain "only move forwards" rule files it below every
    success and the failure is never shown to the host."""
    db, ev, rec = _recipient_with_message(wa)
    for ack in (waha.ACK_SERVER, waha.ACK_DEVICE, waha.ACK_ERROR):
        _signed(wa, {"event": "message.ack", "session": "utest",
                     "payload": {"id": rec.wa_message_id, "ack": ack}})
    db2, _ = _db_and_user()
    assert db2.get(Recipient, rec.id).wa_ack == waha.ACK_ERROR
    assert "deliver it" in wa.get(f"/events/{ev}").text


def test_the_api_surface_is_not_published(wa):
    """An unauthenticated map of the routes only advertises the webhook and the
    account endpoints."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert wa.get(path).status_code == 404, path
