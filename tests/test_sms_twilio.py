"""The Twilio provider.

Nothing here touches the network: every test injects an httpx.MockTransport, the
same way tests/test_waha.py does. A test that could reach api.twilio.com would
be a test that can spend money.

What matters beyond the happy path is that a failure is never mistaken for a
send. A recipient flipped to 'sent' for a text that never left is worse than a
visible error, because nothing will ever retry it.
"""

import httpx
import pytest

from kith.config import Settings
from kith.services import sms
from kith.services.sms_twilio import API_ROOT, TwilioProvider

SID, TOKEN = "AC0123456789abcdef", "tok_secret"
TO, TEXT = "+15551110000", "Hi Mara - it's Ilia."


def _provider(handler, **kw):
    return TwilioProvider(
        SID, TOKEN, transport=httpx.MockTransport(handler), **kw
    )


def _ok(sid="SM123", **extra):
    def handler(request):
        handler.request = request
        return httpx.Response(201, json={"sid": sid, "status": "queued", **extra})

    handler.request = None
    return handler


# --- the request ---------------------------------------------------------------

def test_a_send_posts_to_the_account_messages_endpoint():
    h = _ok()
    _provider(h, from_number="+15550001234").send(TO, TEXT)
    assert str(h.request.url) == f"{API_ROOT}/Accounts/{SID}/Messages.json"
    assert h.request.method == "POST"


def test_it_authenticates_with_the_sid_and_token():
    import base64

    h = _ok()
    _provider(h, from_number="+15550001234").send(TO, TEXT)
    expected = base64.b64encode(f"{SID}:{TOKEN}".encode()).decode()
    assert h.request.headers["authorization"] == f"Basic {expected}"


def test_it_sends_the_number_and_the_body_form_encoded():
    from urllib.parse import parse_qs

    h = _ok()
    _provider(h, from_number="+15550001234").send(TO, TEXT)
    form = parse_qs(h.request.content.decode())
    assert form["To"] == [TO]
    assert form["Body"] == [TEXT]
    assert form["From"] == ["+15550001234"]
    assert "MessagingServiceSid" not in form


def test_a_messaging_service_replaces_the_from_number():
    from urllib.parse import parse_qs

    h = _ok()
    _provider(h, messaging_service_sid="MG999").send(TO, TEXT)
    form = parse_qs(h.request.content.decode())
    assert form["MessagingServiceSid"] == ["MG999"]
    assert "From" not in form


def test_a_messaging_service_wins_when_both_are_set():
    """It is the more specific instruction, and it picks the number itself."""
    from urllib.parse import parse_qs

    h = _ok()
    _provider(h, from_number="+15550001234", messaging_service_sid="MG999").send(TO, TEXT)
    form = parse_qs(h.request.content.decode())
    assert form["MessagingServiceSid"] == ["MG999"]
    assert "From" not in form


def test_the_message_text_is_sent_exactly_as_given():
    """No truncation, no re-encoding: segment counting is the caller's business
    and silently trimming a text would drop the invitation link off the end."""
    from urllib.parse import parse_qs

    long_text = "x" * 400 + " https://kith.example/i/abc"
    h = _ok()
    _provider(h, from_number="+1").send(TO, long_text)
    assert parse_qs(h.request.content.decode())["Body"] == [long_text]


# --- the response --------------------------------------------------------------

def test_a_successful_send_returns_the_sid_as_the_message_id():
    """Kept so a delivery receipt arriving later can find its recipient."""
    res = _provider(_ok(sid="SM_abc"), from_number="+1").send(TO, TEXT)
    assert res == sms.SmsResult(message_id="SM_abc")


def test_a_success_with_no_sid_is_still_a_success():
    def handler(request):
        return httpx.Response(201, json={"status": "queued"})

    assert _provider(handler, from_number="+1").send(TO, TEXT).message_id is None


def test_a_body_that_is_not_json_does_not_crash_a_successful_send():
    def handler(request):
        return httpx.Response(201, text="<html>ok</html>")

    assert _provider(handler, from_number="+1").send(TO, TEXT).message_id is None


def test_a_201_carrying_an_error_code_is_not_treated_as_sent():
    """Twilio can accept the request and refuse the message in the same breath.

    Reporting that as success flips the recipient to 'sent' for a text that
    never went, and nothing retries a 'sent' row.
    """
    def handler(request):
        return httpx.Response(201, json={
            "sid": "SM1", "status": "failed",
            "error_code": 21610, "error_message": "Attempt to send to unsubscribed recipient",
        })

    with pytest.raises(sms.SmsError, match="21610"):
        _provider(handler, from_number="+1").send(TO, TEXT)


# --- failures ------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials_raise_an_auth_error(status):
    """Distinct because retrying will never help — the batch should stop."""
    def handler(request):
        return httpx.Response(status, json={"code": 20003, "message": "Authenticate"})

    with pytest.raises(sms.SmsAuthError):
        _provider(handler, from_number="+1").send(TO, TEXT)


def test_a_400_raises_an_sms_error_carrying_twilios_own_words():
    def handler(request):
        return httpx.Response(400, json={
            "code": 21211, "message": "The 'To' number is not a valid phone number",
        })

    with pytest.raises(sms.SmsError) as e:
        _provider(handler, from_number="+1").send("nonsense", TEXT)
    assert "21211" in str(e.value)
    assert "not a valid phone number" in str(e.value)
    assert not isinstance(e.value, sms.SmsAuthError)


def test_a_500_raises_an_sms_error():
    def handler(request):
        return httpx.Response(500, text="upstream exploded")

    with pytest.raises(sms.SmsError, match="500"):
        _provider(handler, from_number="+1").send(TO, TEXT)


def test_an_html_error_page_is_reported_but_capped():
    """A proxy's error page shouldn't put a kilobyte of markup in the logs."""
    def handler(request):
        return httpx.Response(502, text="<html>" + "e" * 5000 + "</html>")

    with pytest.raises(sms.SmsError) as e:
        _provider(handler, from_number="+1").send(TO, TEXT)
    assert len(str(e.value)) < 300


def test_a_timeout_raises_sms_timeout():
    def handler(request):
        raise httpx.ConnectTimeout("too slow")

    with pytest.raises(sms.SmsTimeout):
        _provider(handler, from_number="+1").send(TO, TEXT)


def test_a_transport_error_raises_sms_error():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(sms.SmsError) as e:
        _provider(handler, from_number="+1").send(TO, TEXT)
    assert not isinstance(e.value, sms.SmsTimeout)


def test_the_auth_token_never_appears_in_an_error_message():
    """Errors reach the logs, and the logs are not a secret store."""
    def handler(request):
        return httpx.Response(400, json={"code": 1, "message": "nope"})

    with pytest.raises(sms.SmsError) as e:
        _provider(handler, from_number="+1").send(TO, TEXT)
    assert TOKEN not in str(e.value)


# --- capabilities and the factory ---------------------------------------------

def test_twilio_reports_that_it_can_post_receipts_and_inbound():
    caps = _provider(_ok(), from_number="+1").capabilities()
    assert caps == sms.SmsCaps(can_receipt=True, can_inbound=True)


def _settings(**kw):
    base = dict(
        sms_enabled=True, sms_provider="twilio",
        sms_twilio_account_sid=SID, sms_twilio_auth_token=TOKEN,
        sms_twilio_from="+15550001234",
    )
    base.update(kw)
    return Settings(**base)


def test_the_factory_returns_a_twilio_provider_when_configured():
    assert isinstance(sms.get_provider(_settings()), TwilioProvider)


def test_the_factory_returns_null_when_the_channel_is_disabled():
    assert isinstance(sms.get_provider(_settings(sms_enabled=False)), sms.NullProvider)


def test_the_factory_returns_null_for_an_unknown_provider_name():
    """A typo in the config should stop the send loudly, not the app at import."""
    assert isinstance(sms.get_provider(_settings(sms_provider="twillio")), sms.NullProvider)


def test_the_factory_passes_the_configured_timeout_through():
    provider = sms.get_provider(_settings(sms_timeout_seconds=7.5))
    assert provider._timeout.read == 7.5


# --- the configured gate -------------------------------------------------------

def test_twilio_needs_a_sid_a_token_and_a_sender():
    """A named but uncredentialed provider is not configured.

    Otherwise the compose box appears, accepts numbers, and fails on the first
    live send — the failure belongs in the operator's config, not in front of a
    host mid-party-planning.
    """
    assert _settings().sms_configured is True
    assert _settings(sms_twilio_account_sid="").sms_configured is False
    assert _settings(sms_twilio_auth_token="").sms_configured is False
    assert _settings(sms_twilio_from="").sms_configured is False


def test_a_messaging_service_alone_counts_as_a_sender():
    s = _settings(sms_twilio_from="", sms_twilio_messaging_service_sid="MG999")
    assert s.sms_configured is True


# --- through the send path -----------------------------------------------------
#
# A real TwilioProvider over a MockTransport, reached through send_event. This
# is the join the unit tests above can't see: that a provider result actually
# lands on the row, and that a provider failure leaves the recipient owed a text.

@pytest.fixture
def live_client(monkeypatch):
    from fastapi.testclient import TestClient

    from kith.config import get_settings

    for k, v in {
        "KITH_SMS_ENABLED": "true", "KITH_SMS_PROVIDER": "twilio",
        "KITH_SMS_TWILIO_ACCOUNT_SID": SID, "KITH_SMS_TWILIO_AUTH_TOKEN": TOKEN,
        "KITH_SMS_TWILIO_FROM": "+15550001234", "KITH_SEND_MODE": "live",
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


def _use(monkeypatch, handler):
    """Point the factory at a real provider wired to this mock transport.

    ``provider_from`` is the seam the send path uses: it resolves the host's
    configuration first — their own row, or the site's settings — and asks for a
    provider for that, so patching the settings-only ``get_provider`` wrapper
    would leave the real Twilio client in place and the test on the network.
    """
    from kith.services import sms as sms_module

    monkeypatch.setattr(
        sms_module, "provider_from",
        lambda config: _provider(handler, from_number="+15550001234"),
    )


def _send_event(client, monkeypatch, handler, *, to="+15551110000"):
    from sqlalchemy import select

    from kith.config import get_settings
    from kith.db.models import Event, Recipient, User
    from kith.db.session import make_engine, make_session_factory
    from kith.services import send as sender

    r = client.post(
        "/events",
        data={"title": "Joe's 3rd Birthday", "event_date": "2099-06-14",
              "event_time": "15:00", "recipients": "", "wa_recipients": "",
              "sms_recipients": to, "block_rsvp": "on", "block_date": "on"},
        follow_redirects=False,
    )
    ev_id = r.headers["location"].split("/events/")[1].split("?")[0]
    _use(monkeypatch, handler)
    db = make_session_factory(make_engine(get_settings().db_path))()
    user = db.execute(select(User)).scalars().first()
    res = sender.send_event(db, db.get(Event, ev_id), user, get_settings())
    rows = db.execute(
        select(Recipient).where(Recipient.event_id == ev_id)
    ).scalars().all()
    return res, rows


def test_a_live_send_stores_the_twilio_sid_on_the_recipient(live_client, monkeypatch):
    res, rows = _send_event(live_client, monkeypatch, _ok(sid="SM_live"))
    assert (res.sms_sent, res.sms_failed, res.sms_blocked) == (1, 0, None)
    assert rows[0].status == "sent" and rows[0].sent_at
    assert rows[0].sms_message_id == "SM_live"


def test_a_live_send_actually_sends_the_composed_invitation(live_client, monkeypatch):
    from urllib.parse import parse_qs

    h = _ok()
    _res, rows = _send_event(live_client, monkeypatch, h)
    body = parse_qs(h.request.content.decode())["Body"][0]
    assert "Joe's 3rd Birthday" in body
    assert f"/i/{rows[0].token}" in body


def test_a_provider_error_leaves_the_recipient_queued_for_a_retry(live_client, monkeypatch):
    """A wrong number costs one recipient, not the send — and not the row."""
    def handler(request):
        return httpx.Response(400, json={"code": 21211, "message": "not a valid number"})

    res, rows = _send_event(live_client, monkeypatch, handler)
    assert (res.sms_sent, res.sms_failed) == (0, 1)
    assert res.sms_blocked is None          # one recipient's problem, not the batch's
    assert rows[0].status == "queued"
    assert rows[0].sms_message_id is None


def test_rejected_credentials_stop_the_whole_batch(live_client, monkeypatch):
    """Every remaining recipient would fail identically, so none are attempted."""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"code": 20003, "message": "Authenticate"})

    res, rows = _send_event(
        live_client, monkeypatch, handler, to="+15551110000\n+15552220000\n+15553330000",
    )
    assert res.sms_blocked == "auth"
    assert len(calls) == 1                                  # stopped after the first
    assert all(r.status == "queued" for r in rows)


def test_a_timeout_costs_one_recipient_and_the_batch_carries_on(live_client, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(201, json={"sid": f"SM{len(seen)}"})

    res, rows = _send_event(
        live_client, monkeypatch, handler, to="+15551110000\n+15552220000",
    )
    assert (res.sms_sent, res.sms_failed) == (1, 1)
    assert res.sms_blocked is None
    assert sorted(r.status for r in rows) == ["queued", "sent"]


# --- ours to fix, not this recipient's: the batch-stopping refusals -----------

@pytest.mark.parametrize(("status", "body"), [
    (404, {"code": 20404, "message": "The requested resource /Accounts//Messages.json "
                                     "was not found"}),
    (400, {"code": 21606, "message": "The From phone number is not a valid, SMS-capable "
                                     "inbound phone number"}),
])
def test_a_setup_problem_is_misconfigured_not_a_bad_recipient(status, body):
    """20404 is an account SID Twilio has never heard of; 21606 a From number that
    isn't ours. Both would fail every guest identically, so one call must do."""
    def handler(request):
        return httpx.Response(status, json=body)

    with pytest.raises(sms.SmsMisconfigured) as e:
        _provider(handler, from_number="+15550001234").send(TO, TEXT)
    assert str(body["code"]) in str(e.value)


def test_a_bad_recipient_number_still_costs_only_that_recipient():
    def handler(request):
        return httpx.Response(400, json={"code": 21211, "message": "not a valid phone number"})

    with pytest.raises(sms.SmsError) as e:
        _provider(handler, from_number="+15550001234").send("nonsense", TEXT)
    assert not isinstance(e.value, sms.SmsMisconfigured | sms.SmsRateLimited)


@pytest.mark.parametrize(("status", "body"), [
    (429, {"code": 20429, "message": "Too Many Requests"}),
    (429, {}),                                   # a proxy's bare 429
    (400, {"code": 20429, "message": "Too Many Requests"}),
])
def test_too_many_requests_stops_the_batch_rather_than_retrying_faster(status, body):
    def handler(request):
        return httpx.Response(status, json=body)

    with pytest.raises(sms.SmsRateLimited):
        _provider(handler, from_number="+15550001234").send(TO, TEXT)


def test_rejected_credentials_keep_twilios_own_words():
    """20003 (bad token) and 20005 (account suspended) are different problems."""
    def handler(request):
        return httpx.Response(401, json={"code": 20005, "message": "Account not active"})

    with pytest.raises(sms.SmsAuthError) as e:
        _provider(handler, from_number="+15550001234").send(TO, TEXT)
    assert "20005" in str(e.value) and "Account not active" in str(e.value)
    assert TOKEN not in str(e.value)


def test_swapped_sender_settings_are_caught_before_any_request():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(201, json={"sid": "SM1"})

    # The service SID where the number should be, and vice versa.
    with pytest.raises(sms.SmsMisconfigured) as e:
        _provider(handler, from_number="MG999").send(TO, TEXT)
    assert "E.164" in str(e.value)
    with pytest.raises(sms.SmsMisconfigured) as e:
        _provider(handler, messaging_service_sid="+15550001234").send(TO, TEXT)
    assert "swapped" in str(e.value)
    assert calls == [], "a request would have cost a paced slot to learn the same thing"


def test_the_connect_timeout_never_exceeds_the_configured_one():
    p = _provider(_ok(), from_number="+15550001234", timeout=2.0)
    assert p._timeout.connect == 2.0
    p = _provider(_ok(), from_number="+15550001234", timeout=20.0)
    assert p._timeout.connect == 5.0
