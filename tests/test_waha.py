"""The WAHA client: payload parsing, the send-time guard rails, and — above all
— that nothing can hang.

Payload shapes here mirror devlikeapro/waha:gows-2026.8.1 (verified against the
running image's OpenAPI spec), so a WAHA upgrade that changes them should break
these tests rather than production.
"""

import json
from datetime import UTC, datetime

import httpx
import pytest

from kith.config import Settings
from kith.services import waha


def _client(handler, **kw) -> waha.WahaClient:
    return waha.WahaClient(
        "http://waha:3000", "test-key", kw.pop("timeout", 20.0),
        transport=httpx.MockTransport(handler),
    )


def _json_route(payload, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.last_request = request
        return httpx.Response(status, json=payload)

    handler.last_request = None
    return handler


# --- parsing ------------------------------------------------------------------

WORKING_SESSION = {
    "name": "uabc",
    "status": "WORKING",
    "me": {
        "id": "15551234567@c.us",
        "pushName": "Ilia",
        "reachoutTimelock": None,
        "messageCapping": None,
    },
}


def test_parses_a_working_session():
    s = waha.SessionState.parse(WORKING_SESSION)
    assert s.name == "uabc"
    assert s.is_working and s.can_send
    assert s.phone == "+15551234567"
    assert s.push_name == "Ilia"


def test_parses_an_unpaired_session():
    s = waha.SessionState.parse({"name": "uabc", "status": "SCAN_QR_CODE", "me": None})
    assert s.is_pairing and not s.is_working and not s.can_send
    assert s.phone is None


@pytest.mark.parametrize("status", ["PASSKEY_REQUIRED", "PASSKEY_CONFIRMATION_REQUIRED"])
def test_passkey_states_count_as_pairing(status):
    # 2026.8.1 added passkey pairing next to the QR flow; the linking UI must not
    # treat these as failures.
    s = waha.SessionState.parse({"name": "u", "status": status})
    assert s.is_pairing and not s.can_send


def test_parses_timelock_and_capping_off_the_session():
    s = waha.SessionState.parse(
        {
            "name": "u",
            "status": "WORKING",
            "me": {
                "id": "1@c.us",
                "pushName": "x",
                "reachoutTimelock": {
                    "isActive": True,
                    "timeEnforcementEnds": 1784477333,
                    "enforcementType": "DEFAULT",
                },
                "messageCapping": {
                    "cappingStatus": "FIRST_WARNING",
                    "totalQuota": 1000,
                    "usedQuota": 640,
                    "cycleEnd": 1785553199,
                },
            },
        }
    )
    assert s.timelock.is_active
    assert s.timelock.ends_at == datetime.fromtimestamp(1784477333, tz=UTC)
    assert s.capping.warning and not s.capping.is_capped
    assert s.capping.remaining == 360
    assert not s.can_send  # WORKING, but restricted


def test_capping_remaining_is_none_when_uncapped():
    c = waha.Capping.parse({"cappingStatus": "NONE", "totalQuota": -1, "usedQuota": 0})
    assert c.remaining is None and not c.is_capped


def test_unknown_capping_status_is_not_treated_as_capped():
    # WhatsApp may add values; only an explicit CAPPED blocks sending.
    c = waha.Capping.parse({"cappingStatus": "SOMETHING_NEW", "totalQuota": 5, "usedQuota": 1})
    assert not c.is_capped and not c.warning


# --- guard rails --------------------------------------------------------------

def test_raise_if_unsendable_reports_timelock_first():
    s = waha.SessionState.parse(
        {
            "name": "u", "status": "WORKING",
            "me": {"id": "1@c.us", "pushName": "x",
                   "reachoutTimelock": {"isActive": True, "timeEnforcementEnds": 1784477333,
                                        "enforcementType": "DEFAULT"}},
        }
    )
    with pytest.raises(waha.Timelocked) as e:
        s.raise_if_unsendable()
    assert e.value.ends_at == datetime.fromtimestamp(1784477333, tz=UTC)


def test_raise_if_unsendable_on_unlinked():
    s = waha.SessionState.parse({"name": "u", "status": "SCAN_QR_CODE"})
    with pytest.raises(waha.NotLinked):
        s.raise_if_unsendable()


def test_raise_if_unsendable_on_capped():
    s = waha.SessionState.parse(
        {"name": "u", "status": "WORKING",
         "me": {"id": "1@c.us", "pushName": "x",
                "messageCapping": {"cappingStatus": "CAPPED", "totalQuota": 100,
                                   "usedQuota": 100, "cycleEnd": 1785553199}}}
    )
    with pytest.raises(waha.Capped):
        s.raise_if_unsendable()


def test_a_healthy_session_does_not_raise():
    waha.SessionState.parse(WORKING_SESSION).raise_if_unsendable()


# --- transport ----------------------------------------------------------------

def test_send_text_posts_the_expected_payload():
    handler = _json_route({"id": "false_15551234567@c.us_AAA"}, status=201)  # WAHA returns 201
    res = _client(handler).send_text("uabc", "+15551234567", "hi there")
    assert res["id"].startswith("false_")
    body = json.loads(handler.last_request.content)
    assert body == {
        "session": "uabc",
        "chatId": "15551234567@c.us",
        "text": "hi there",
        "linkPreview": True,
    }
    assert handler.last_request.headers["X-Api-Key"] == "test-key"


def test_send_text_prefers_a_resolved_chat_id():
    # WhatsApp is migrating accounts to @lid ids, so check-exists' answer wins.
    handler = _json_route({"id": "x"}, status=201)
    _client(handler).send_text("u", "+15551234567", "hi", chat_id="123456@lid")
    assert json.loads(handler.last_request.content)["chatId"] == "123456@lid"


def test_check_exists_returns_the_canonical_chat_id():
    handler = _json_route({"numberExists": True, "chatId": "15551234567@c.us"})
    got = _client(handler).check_exists("u", "+15551234567")
    assert got.exists and got.chat_id == "15551234567@c.us"
    assert "phone=15551234567" in str(handler.last_request.url)  # digits, no "+"


def test_check_exists_false_for_a_number_not_on_whatsapp():
    got = _client(_json_route({"numberExists": False})).check_exists("u", "+15551234567")
    assert not got.exists and got.chat_id is None


def test_bad_api_key_raises_auth_error():
    handler = _json_route({"message": "Unauthorized", "statusCode": 401}, status=401)
    with pytest.raises(waha.WahaAuthError):
        _client(handler).get_session("uabc")


def test_missing_session_raises_not_found():
    handler = _json_route({"message": "Session not found", "statusCode": 404}, status=404)
    with pytest.raises(waha.WahaNotFound):
        _client(handler).get_session("nosuch")


def test_find_session_maps_404_to_none():
    handler = _json_route({"message": "Session not found"}, status=404)
    assert _client(handler).find_session("nosuch") is None


def test_server_error_raises_generic_waha_error():
    handler = _json_route({"statusCode": 500}, status=500)
    with pytest.raises(waha.WahaError):
        _client(handler).get_session("u")


def test_non_json_response_is_an_error_not_a_crash():
    def handler(request):
        return httpx.Response(200, text="<html>nope</html>")

    with pytest.raises(waha.WahaError):
        _client(handler).version()


def test_a_hanging_waha_call_times_out_rather_than_blocking():
    """The one that matters. Against a session that isn't paired, real WAHA does
    not fail fast — sendText and check-exists hang, and timelock blocks for
    minutes. If this ever regresses, one wedged session can wedge the reminder
    sweep, so every call must be bounded."""

    def handler(request):
        raise httpx.ReadTimeout("simulated hang", request=request)

    client = _client(handler, timeout=0.05)
    for call in (
        lambda: client.send_text("u", "+15551234567", "hi"),
        lambda: client.check_exists("u", "+15551234567"),
        lambda: client.refresh_timelock("u"),
        lambda: client.get_session("u"),
    ):
        with pytest.raises(waha.WahaTimeout):
            call()


def test_healthy_is_false_when_waha_is_unreachable():
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    assert _client(handler).healthy() is False


def test_ensure_session_creates_when_absent():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(404, json={"message": "Session not found"})
        return httpx.Response(201, json={"name": "uabc", "status": "STARTING"})

    state = _client(handler).ensure_session("uabc")
    assert state.status == "STARTING"
    assert ("POST", "/api/sessions") in calls


def test_ensure_session_restarts_a_failed_one():
    """A FAILED session needs *restart*, not start.

    This is the whole bug: `start` answers 201 and logs "Session is already
    running", because the session object really is running — it's the WhatsApp
    connection underneath that died. Pressing "Link WhatsApp" then appears to work
    while leaving the host stranded on "that pairing attempt didn't finish".
    """
    seen = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"name": "uabc", "status": "FAILED"})
        seen.append(request.url.path)
        return httpx.Response(200, json={"name": "uabc", "status": "SCAN_QR_CODE"})

    assert _client(handler).ensure_session("uabc").status == "SCAN_QR_CODE"
    assert seen == ["/api/sessions/uabc/restart"]


def test_ensure_session_starts_a_stopped_one():
    """STOPPED is the case start actually is for."""
    seen = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"name": "uabc", "status": "STOPPED"})
        seen.append(request.url.path)
        return httpx.Response(200, json={"name": "uabc", "status": "STARTING"})

    assert _client(handler).ensure_session("uabc").status == "STARTING"
    assert seen == ["/api/sessions/uabc/start"]


def test_ensure_session_leaves_a_pairing_session_alone():
    """Restarting mid-pairing would throw away the QR being scanned, or the code
    being typed."""
    def handler(request):
        assert request.method == "GET", "must not disturb a session that's pairing"
        return httpx.Response(200, json={"name": "uabc", "status": "SCAN_QR_CODE"})

    assert _client(handler).ensure_session("uabc").is_pairing


def test_ensure_session_leaves_a_working_one_alone():
    def handler(request):
        assert request.method == "GET", "must not restart a working session"
        return httpx.Response(200, json=WORKING_SESSION)

    assert _client(handler).ensure_session("uabc").is_working


def test_unlink_is_quiet_when_the_session_is_already_gone():
    def handler(request):
        return httpx.Response(404, json={"message": "Session not found"})

    _client(handler).unlink("uabc")  # must not raise — account deletion depends on it


def test_unlink_logs_out_then_deletes():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={})

    _client(handler).unlink("uabc")
    assert seen == [("POST", "/api/sessions/uabc/logout"), ("DELETE", "/api/sessions/uabc")]


def test_qr_png_is_passed_through_as_bytes():
    def handler(request):
        assert request.url.params["format"] == "image"  # WAHA requires it
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nfake")

    assert _client(handler).qr_png("uabc").startswith(b"\x89PNG")


def test_qr_raw_returns_the_pairing_link():
    handler = _json_route({"value": "https://wa.me/settings/linked_devices#2@abc"})
    assert _client(handler).qr_raw("uabc").startswith("https://wa.me/")


# --- settings wiring ----------------------------------------------------------

def test_whatsapp_is_off_by_default():
    s = Settings()
    assert s.whatsapp_enabled is False
    assert s.whatsapp_configured is False


def test_whatsapp_configured_needs_a_key():
    assert not Settings(whatsapp_enabled=True, waha_api_key="").whatsapp_configured
    assert Settings(whatsapp_enabled=True, waha_api_key="k").whatsapp_configured


def test_client_from_settings_uses_configured_url_and_timeout():
    s = Settings(waha_url="http://waha:3000/", waha_api_key="k", waha_timeout_seconds=7.5)
    c = waha.WahaClient.from_settings(s)
    assert c._base == "http://waha:3000"  # trailing slash trimmed
    assert c._timeout.read == 7.5


def test_wrong_session_state_maps_to_not_linked():
    """WAHA's real 422 when a session isn't WORKING (observed against 2026.8.1).
    It means "re-link", not "the send broke", so the send path can say so."""
    handler = _json_route(
        {
            "error": "Session status is not as expected. Try again later or restart the session",
            "session": "uabc",
            "status": "SCAN_QR_CODE",
            "expected": ["WORKING"],
        },
        status=422,
    )
    with pytest.raises(waha.NotLinked) as e:
        _client(handler).send_text("uabc", "+15551234567", "hi")
    assert "SCAN_QR_CODE" in str(e.value)


def test_an_unrelated_422_stays_a_generic_error():
    handler = _json_route({"message": "validation failed"}, status=422)
    with pytest.raises(waha.WahaError) as e:
        _client(handler).send_text("uabc", "+15551234567", "hi")
    assert not isinstance(e.value, waha.NotLinked)
