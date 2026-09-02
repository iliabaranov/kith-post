"""The capcom6 Android SMS gateway provider.

No test reaches a device or the network: every one injects an
httpx.MockTransport, the same way test_sms_twilio and test_waha do.

The shape here was checked against the project's docs (docs.sms-gate.app), and
the one thing worth pinning is that there are TWO of them — the app's on-device
Local Server answers at /message, and the self-hosted relay at
/3rdparty/v1/messages. Getting that wrong is a 404, which is why it has a
setting, a constant apiece, and an error message that names both.
"""

import httpx
import pytest

from kith.config import Settings
from kith.services import sms
from kith.services.sms_gateway import (
    LOCAL_SERVER_PATH,
    RELAY_PATH,
    AndroidGatewayProvider,
)

BASE = "http://192.168.1.50:8080"
USER, PASSWORD = "sms", "gateway-secret"
TO, TEXT = "+15551110000", "Hi Mara - it's Ilia."


def _provider(handler, **kw):
    return AndroidGatewayProvider(
        BASE, USER, PASSWORD, transport=httpx.MockTransport(handler), **kw
    )


def _ok(msg_id="msg-1", state="Pending"):
    def handler(request):
        handler.request = request
        return httpx.Response(202, json={"id": msg_id, "state": state})

    handler.request = None
    return handler


# --- the request ---------------------------------------------------------------

def test_a_send_posts_to_the_local_server_path_by_default():
    h = _ok()
    _provider(h).send(TO, TEXT)
    assert str(h.request.url) == f"{BASE}{LOCAL_SERVER_PATH}"
    assert h.request.method == "POST"


def test_the_relay_path_is_a_setting_not_a_second_provider():
    h = _ok()
    _provider(h, path=RELAY_PATH).send(TO, TEXT)
    assert str(h.request.url) == f"{BASE}{RELAY_PATH}"


def test_a_trailing_slash_on_the_base_url_does_not_double_up():
    h = _ok()
    AndroidGatewayProvider(
        BASE + "/", USER, PASSWORD, transport=httpx.MockTransport(h)
    ).send(TO, TEXT)
    assert str(h.request.url) == f"{BASE}{LOCAL_SERVER_PATH}"


def test_it_authenticates_with_basic_auth():
    """The app's Local Server supports nothing else."""
    import base64

    h = _ok()
    _provider(h).send(TO, TEXT)
    expected = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    assert h.request.headers["authorization"] == f"Basic {expected}"


def test_it_sends_the_documented_json_body():
    import json

    h = _ok()
    _provider(h).send(TO, TEXT)
    assert h.request.headers["content-type"].startswith("application/json")
    assert json.loads(h.request.content) == {
        "textMessage": {"text": TEXT},
        "phoneNumbers": [TO],
    }


def test_one_recipient_per_call_even_though_the_field_is_a_list():
    """The send path paces and commits per recipient; batching would make one
    failure ambiguous across several people."""
    import json

    h = _ok()
    _provider(h).send(TO, TEXT)
    assert json.loads(h.request.content)["phoneNumbers"] == [TO]


def test_a_device_id_is_sent_only_when_configured():
    import json

    h = _ok()
    _provider(h).send(TO, TEXT)
    assert "deviceId" not in json.loads(h.request.content)

    h2 = _ok()
    _provider(h2, device_id="dev-7").send(TO, TEXT)
    assert json.loads(h2.request.content)["deviceId"] == "dev-7"


def test_the_text_is_sent_exactly_as_given():
    import json

    long_text = "x" * 400 + " https://kith.example/i/abc"
    h = _ok()
    _provider(h).send(TO, long_text)
    assert json.loads(h.request.content)["textMessage"]["text"] == long_text


# --- the response --------------------------------------------------------------

def test_a_successful_send_returns_the_gateway_id():
    assert _provider(_ok(msg_id="zXDYfTmTVf3")).send(TO, TEXT) == sms.SmsResult(
        message_id="zXDYfTmTVf3"
    )


@pytest.mark.parametrize("state", ["Pending", "Sent", "Delivered", "Processed"])
def test_every_in_flight_state_counts_as_accepted(state):
    assert _provider(_ok(state=state)).send(TO, TEXT).message_id == "msg-1"


@pytest.mark.parametrize("state", ["Failed", "Cancelled"])
def test_a_refusal_wearing_a_success_code_is_not_treated_as_sent(state):
    """Same trap as Twilio's error_code in a 201: reporting it as success flips
    the recipient to 'sent' for a text that never went, and nothing retries a
    'sent' row."""
    with pytest.raises(sms.SmsError, match=state):
        _provider(_ok(state=state)).send(TO, TEXT)


def test_a_response_with_no_state_is_accepted():
    """Not every version answers with one, and a 2xx with an id is a send."""
    def handler(request):
        return httpx.Response(202, json={"id": "msg-9"})

    assert _provider(handler).send(TO, TEXT).message_id == "msg-9"


def test_a_success_with_no_id_is_still_a_success():
    def handler(request):
        return httpx.Response(202, json={"state": "Pending"})

    assert _provider(handler).send(TO, TEXT).message_id is None


def test_a_body_that_is_not_json_does_not_crash_a_successful_send():
    def handler(request):
        return httpx.Response(200, text="OK")

    assert _provider(handler).send(TO, TEXT).message_id is None


# --- failures ------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials_raise_an_auth_error(status):
    """Distinct so the send path stops the batch instead of trying everyone."""
    def handler(request):
        return httpx.Response(status, text="unauthorized")

    with pytest.raises(sms.SmsAuthError):
        _provider(handler).send(TO, TEXT)


def test_a_404_names_both_paths_because_that_is_the_likely_mistake():
    """A relay URL with the on-device path, or the reverse — by far the most
    likely misconfiguration, and a bare 404 leaves the operator guessing."""
    def handler(request):
        return httpx.Response(404, text="Not Found")

    with pytest.raises(sms.SmsError) as e:
        _provider(handler, path=LOCAL_SERVER_PATH).send(TO, TEXT)
    msg = str(e.value)
    assert LOCAL_SERVER_PATH in msg and RELAY_PATH in msg
    assert "KITH_SMS_GATEWAY_PATH" in msg
    assert not isinstance(e.value, sms.SmsAuthError)


def test_a_400_raises_an_sms_error():
    def handler(request):
        return httpx.Response(400, text="bad phone number")

    with pytest.raises(sms.SmsError, match="400"):
        _provider(handler).send("nonsense", TEXT)


def test_a_500_raises_an_sms_error_and_is_capped():
    def handler(request):
        return httpx.Response(500, text="e" * 5000)

    with pytest.raises(sms.SmsError) as e:
        _provider(handler).send(TO, TEXT)
    assert len(str(e.value)) < 300


def test_a_timeout_raises_sms_timeout():
    """The common case: the phone is asleep or off the network."""
    def handler(request):
        raise httpx.ConnectTimeout("no answer")

    with pytest.raises(sms.SmsTimeout):
        _provider(handler).send(TO, TEXT)


def test_an_unreachable_phone_raises_sms_error():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(sms.SmsError) as e:
        _provider(handler).send(TO, TEXT)
    assert not isinstance(e.value, sms.SmsTimeout)


def test_the_password_never_appears_in_an_error_message():
    def handler(request):
        return httpx.Response(400, text="nope")

    with pytest.raises(sms.SmsError) as e:
        _provider(handler).send(TO, TEXT)
    assert PASSWORD not in str(e.value)


# --- capabilities and the factory ---------------------------------------------

def test_the_gateway_reports_receipts_and_inbound():
    assert _provider(_ok()).capabilities() == sms.SmsCaps(
        can_receipt=True, can_inbound=True
    )


def _settings(**kw):
    base = dict(
        sms_enabled=True, sms_provider="gateway",
        sms_gateway_url=BASE, sms_gateway_user=USER, sms_gateway_pass=PASSWORD,
    )
    base.update(kw)
    return Settings(**base)


def test_the_factory_returns_the_gateway_provider_when_configured():
    assert isinstance(sms.get_provider(_settings()), AndroidGatewayProvider)


def test_the_factory_threads_the_path_the_device_id_and_the_timeout_through():
    p = sms.get_provider(_settings(
        sms_gateway_path=RELAY_PATH, sms_gateway_device_id="dev-3",
        sms_timeout_seconds=9.0,
    ))
    assert p._url == f"{BASE}{RELAY_PATH}"
    assert p._device_id == "dev-3"
    assert p._timeout.read == 9.0


def test_the_factory_returns_null_when_the_channel_is_disabled():
    assert isinstance(sms.get_provider(_settings(sms_enabled=False)), sms.NullProvider)


def test_choosing_twilio_does_not_get_you_the_gateway():
    """Both are configured; the setting decides, not the order of the ifs."""
    from kith.services.sms_twilio import TwilioProvider

    s = _settings(
        sms_provider="twilio", sms_twilio_account_sid="AC1",
        sms_twilio_auth_token="tok", sms_twilio_from="+1",
    )
    assert isinstance(sms.get_provider(s), TwilioProvider)


# --- the configured gate -------------------------------------------------------

def test_the_gateway_needs_a_url_and_credentials():
    """Basic auth is all the Local Server supports, so an unauthenticated call
    is a 401 rather than a send — the credentials matter as much as the URL."""
    assert _settings().sms_configured is True
    assert _settings(sms_gateway_url="").sms_configured is False
    assert _settings(sms_gateway_user="").sms_configured is False
    assert _settings(sms_gateway_pass="").sms_configured is False


def test_the_gateway_is_not_configured_when_the_channel_is_off():
    assert _settings(sms_enabled=False).sms_configured is False


# --- through the send path -----------------------------------------------------

@pytest.fixture
def live_client(monkeypatch):
    from fastapi.testclient import TestClient

    from kith.config import get_settings

    for k, v in {
        "KITH_SMS_ENABLED": "true", "KITH_SMS_PROVIDER": "gateway",
        "KITH_SMS_GATEWAY_URL": BASE, "KITH_SMS_GATEWAY_USER": USER,
        "KITH_SMS_GATEWAY_PASS": PASSWORD, "KITH_SEND_MODE": "live",
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


def _send_event(client, monkeypatch, handler, *, to=TO):
    from sqlalchemy import select

    from kith.config import get_settings
    from kith.db.models import Event, Recipient, User
    from kith.db.session import make_engine, make_session_factory
    from kith.services import send as sender
    from kith.services import sms as sms_module

    r = client.post(
        "/events",
        data={"title": "Joe's 3rd Birthday", "event_date": "2099-06-14",
              "event_time": "15:00", "recipients": "", "wa_recipients": "",
              "sms_recipients": to, "block_rsvp": "on", "block_date": "on"},
        follow_redirects=False,
    )
    ev_id = r.headers["location"].split("/events/")[1].split("?")[0]
    monkeypatch.setattr(sms_module, "get_provider", lambda settings: _provider(handler))
    db = make_session_factory(make_engine(get_settings().db_path))()
    user = db.execute(select(User)).scalars().first()
    res = sender.send_event(db, db.get(Event, ev_id), user, get_settings())
    rows = db.execute(
        select(Recipient).where(Recipient.event_id == ev_id)
    ).scalars().all()
    return res, rows


def test_a_live_send_stores_the_gateway_id_on_the_recipient(live_client, monkeypatch):
    res, rows = _send_event(live_client, monkeypatch, _ok(msg_id="msg-live"))
    assert (res.sms_sent, res.sms_failed, res.sms_blocked) == (1, 0, None)
    assert rows[0].status == "sent" and rows[0].sent_at
    assert rows[0].sms_message_id == "msg-live"


def test_a_live_send_actually_sends_the_composed_invitation(live_client, monkeypatch):
    import json

    h = _ok()
    _res, rows = _send_event(live_client, monkeypatch, h)
    body = json.loads(h.request.content)["textMessage"]["text"]
    assert "Joe's 3rd Birthday" in body
    assert f"/i/{rows[0].token}" in body


def test_an_unreachable_phone_leaves_the_recipient_queued(live_client, monkeypatch):
    """The phone comes back; the invitation should still be owed when it does."""
    def handler(request):
        raise httpx.ConnectTimeout("phone asleep")

    res, rows = _send_event(live_client, monkeypatch, handler)
    assert (res.sms_sent, res.sms_failed) == (0, 1)
    assert res.sms_blocked is None
    assert rows[0].status == "queued"


def test_rejected_credentials_stop_the_whole_batch(live_client, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, text="unauthorized")

    res, rows = _send_event(
        live_client, monkeypatch, handler, to=f"{TO}\n+15552220000\n+15553330000",
    )
    assert res.sms_blocked == "auth"
    assert len(calls) == 1
    assert all(r.status == "queued" for r in rows)
