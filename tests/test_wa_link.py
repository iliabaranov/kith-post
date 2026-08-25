"""Linking a host's WhatsApp account.

The gate is the point: nothing may exist in WAHA before the host has accepted
that this uses an unofficial client and their account could be banned. These
tests drive the routes with a fake WAHA so no container is needed.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kith.config import get_settings
from kith.db.models import User
from kith.services import wa_session as link
from kith.services import waha


class FakeWaha:
    """A stand-in WAHA whose session status the test drives directly."""

    def __init__(self, status=None, phone=None):
        self.status = status          # None = WAHA has no such session
        self.phone = phone
        self.calls = []
        self.timelock = None
        self.capping = None
        self.code_error = None

    # -- the surface wa_session uses --
    def _state(self):
        if self.status is None:
            return None
        return waha.SessionState(
            name="utest", status=self.status, phone=self.phone,
            timelock=self.timelock, capping=self.capping,
        )

    def find_session(self, name):
        self.calls.append(("find", name))
        return self._state()

    def get_session(self, name):
        self.calls.append(("get", name))
        st = self._state()
        if st is None:
            raise waha.WahaNotFound("no session")
        return st

    def ensure_session(self, name):
        self.calls.append(("ensure", name))
        if self.status is None:
            self.status = waha.STATUS_SCAN_QR
        elif self.status == waha.STATUS_FAILED:
            return self.restart_session(name)   # start would be a no-op
        return self._state()

    def ensure_webhooks(self, name):
        self.calls.append(("webhooks", name))
        return False          # already pointed at us

    def qr_png(self, name):
        self.calls.append(("qr", name))
        return b"\x89PNG\r\n\x1a\nfake-qr"

    def restart_session(self, name):
        self.calls.append(("restart", name))
        self.status = waha.STATUS_SCAN_QR   # what a real restart does
        return self._state()

    def request_pairing_code(self, name, phone):
        self.calls.append(("code", name, phone))
        if self.code_error:
            raise self.code_error
        return "WW5J-87T4"

    def unlink(self, name):
        self.calls.append(("unlink", name))
        self.status, self.phone = None, None


@pytest.fixture
def wa(monkeypatch):
    """The channel enabled, a signed-in host, and a fake WAHA behind it."""
    monkeypatch.setenv("KITH_WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("KITH_WAHA_API_KEY", "test-key")
    get_settings.cache_clear()
    fake = FakeWaha()
    monkeypatch.setattr(link, "client", lambda settings: fake)
    try:
        from kith.web.app import create_app

        with TestClient(create_app()) as c:
            c.post("/auth/dev-login")
            yield c, fake
    finally:
        get_settings.cache_clear()


def _user():
    from kith.db.session import make_engine, make_session_factory

    db = make_session_factory(make_engine(get_settings().db_path))()
    return db, db.execute(select(User)).scalars().first()


# --- the gate -----------------------------------------------------------------

def test_the_warning_comes_first_and_names_the_risk(wa):
    client, fake = wa
    body = client.get("/account/whatsapp").text
    assert "against WhatsApp's terms of service" in body
    assert "restrict or ban" in body
    assert "I understand" in body
    # No link button, and above all nothing created in WAHA.
    assert "Link WhatsApp</button>" not in body
    assert fake.calls == []


def test_linking_is_refused_until_the_warning_is_accepted(wa):
    client, fake = wa
    client.post("/account/whatsapp/link", follow_redirects=False)
    assert not any(c[0] == "ensure" for c in fake.calls), "created a session without consent"
    db, user = _user()
    assert user.wa_session is None


def test_accepting_the_warning_is_recorded_once(wa):
    client, _ = wa
    client.post("/account/whatsapp/acknowledge", follow_redirects=False)
    db, user = _user()
    first = user.wa_risk_ack_at
    assert first is not None
    client.post("/account/whatsapp/acknowledge", follow_redirects=False)
    db2, user2 = _user()
    assert user2.wa_risk_ack_at == first  # not re-stamped


def test_after_accepting_the_page_offers_the_link(wa):
    client, _ = wa
    client.post("/account/whatsapp/acknowledge")
    body = client.get("/account/whatsapp").text
    assert "Link WhatsApp" in body
    assert "I understand" not in body


# --- pairing ------------------------------------------------------------------

def _acked_and_linking(client):
    client.post("/account/whatsapp/acknowledge")
    client.post("/account/whatsapp/link", follow_redirects=False)


def test_linking_creates_a_session_and_shows_the_qr(wa):
    client, fake = wa
    _acked_and_linking(client)
    assert ("ensure", "u" + _user()[1].id.replace("-", "")) in fake.calls
    body = client.get("/account/whatsapp").text
    assert "/account/whatsapp/qr.png" in body
    assert "Scan this code" in body
    assert "Linked devices" in body  # tells them where to look on the phone


def test_the_qr_is_proxied_and_never_cached(wa):
    client, fake = wa
    _acked_and_linking(client)
    r = client.get("/account/whatsapp/qr.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")
    assert "no-store" in r.headers["cache-control"]
    assert ("qr", "utest") in [(c[0], "utest") for c in fake.calls if c[0] == "qr"]


def test_the_passkey_states_are_explained_not_treated_as_failure(wa):
    client, fake = wa
    client.post("/account/whatsapp/acknowledge")
    fake.status = waha.STATUS_PASSKEY
    client.post("/account/whatsapp/link", follow_redirects=False)
    body = client.get("/account/whatsapp").text
    assert "passkey" in body.lower()
    assert "pairing attempt" not in body


def test_a_failed_session_offers_another_go(wa):
    client, fake = wa
    _acked_and_linking(client)
    fake.status = waha.STATUS_FAILED   # an unpaired session drifts here on its own
    body = client.get("/account/whatsapp").text
    assert "pairing attempt" in body and "Try linking again" in body
    assert "Link WhatsApp" in body


def test_the_state_endpoint_reports_pairing_progress(wa):
    client, fake = wa
    _acked_and_linking(client)
    assert client.get("/account/whatsapp/state").json() == {
        "status": waha.STATUS_SCAN_QR, "linked": False, "pairing": True,
        "number": None, "prompt": "Scan this code with WhatsApp on your phone.",
    }
    fake.status, fake.phone = waha.STATUS_WORKING, "+15551234567"
    s = client.get("/account/whatsapp/state").json()
    assert s["linked"] is True and s["number"] == "+15551234567"


def test_a_completed_pairing_is_remembered(wa):
    client, fake = wa
    _acked_and_linking(client)
    fake.status, fake.phone = waha.STATUS_WORKING, "+15551234567"
    body = client.get("/account/whatsapp").text
    assert "+15551234567" in body
    db, user = _user()
    assert user.wa_status == waha.STATUS_WORKING
    assert user.wa_number == "+15551234567"
    assert user.wa_linked_at is not None


def test_the_linked_number_is_encrypted_at_rest(wa):
    """It's a phone number on someone's account page — same treatment as email."""
    import sqlite3

    client, fake = wa
    _acked_and_linking(client)
    fake.status, fake.phone = waha.STATUS_WORKING, "+15551234567"
    client.get("/account/whatsapp")
    raw = sqlite3.connect(get_settings().db_path).execute(
        "select wa_number from users"
    ).fetchone()[0]
    assert raw and "+15551234567" not in raw


# --- restrictions -------------------------------------------------------------

def test_a_timelock_is_explained_and_says_not_to_relink(wa):
    client, fake = wa
    _acked_and_linking(client)
    fake.status, fake.phone = waha.STATUS_WORKING, "+15551234567"
    fake.timelock = waha.Timelock.parse(
        {"isActive": True, "timeEnforcementEnds": 1784477333, "enforcementType": "DEFAULT"}
    )
    body = client.get("/account/whatsapp").text
    assert "paused new conversations" in body
    assert "re-linking would only make it look worse" in body
    # It stops the whole batch — it does not spare people already messaged.
    assert "messaged before still go out" not in body
    assert "Email is unaffected" in body
    db, user = _user()
    assert user.wa_timelock_until is not None


def test_a_lapsed_timelock_stops_being_shown(wa):
    client, fake = wa
    _acked_and_linking(client)
    fake.status = waha.STATUS_WORKING
    fake.timelock = waha.Timelock.parse(
        {"isActive": True, "timeEnforcementEnds": 1784477333, "enforcementType": "DEFAULT"}
    )
    client.get("/account/whatsapp")
    fake.timelock = waha.Timelock.parse(
        {"isActive": False, "timeEnforcementEnds": None, "enforcementType": "DEFAULT"}
    )
    body = client.get("/account/whatsapp").text
    assert "paused new conversations" not in body
    db, user = _user()
    assert user.wa_timelock_until is None


def test_nearing_the_new_chat_quota_is_surfaced(wa):
    client, fake = wa
    _acked_and_linking(client)
    fake.status = waha.STATUS_WORKING
    fake.capping = waha.Capping.parse(
        {"cappingStatus": "SECOND_WARNING", "totalQuota": 1000, "usedQuota": 940,
         "cycleEnd": 1785553199}
    )
    body = client.get("/account/whatsapp").text
    assert "near WhatsApp's limit" in body
    assert "940 of 1000" in body


# --- unlinking ----------------------------------------------------------------

def test_unlinking_clears_our_side_and_deletes_the_session(wa):
    client, fake = wa
    _acked_and_linking(client)
    fake.status, fake.phone = waha.STATUS_WORKING, "+15551234567"
    client.get("/account/whatsapp")
    client.post("/account/whatsapp/unlink", follow_redirects=False)
    assert ("unlink", "utest") in [(c[0], c[1]) for c in fake.calls if c[0] == "unlink"]
    db, user = _user()
    assert user.wa_session is None
    assert user.wa_number is None
    assert user.wa_status is None
    assert user.wa_linked_at is None


def test_unlinking_works_even_when_waha_is_unreachable(monkeypatch, wa):
    """A host asking to unlink must not be blocked by a service they can't see."""
    client, fake = wa
    _acked_and_linking(client)
    fake.status = waha.STATUS_WORKING
    client.get("/account/whatsapp")

    def boom(name):
        raise waha.WahaTimeout("WAHA is down")

    monkeypatch.setattr(fake, "unlink", boom)
    client.post("/account/whatsapp/unlink", follow_redirects=False)
    db, user = _user()
    assert user.wa_session is None and user.wa_status is None


# --- availability + errors ----------------------------------------------------

def test_the_page_is_absent_when_the_channel_is_off():
    get_settings.cache_clear()
    try:
        from kith.web.app import create_app

        with TestClient(create_app()) as c:
            c.post("/auth/dev-login")
            r = c.get("/account/whatsapp", follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/account"
    finally:
        get_settings.cache_clear()


def test_the_account_page_hides_whatsapp_when_the_channel_is_off():
    # A fresh app, built with the channel off. The module-level `app` captures
    # settings at import time, and in this file that import happens inside the
    # channel-enabled fixture — so relying on it would make this depend on order.
    get_settings.cache_clear()
    try:
        from kith.web.app import create_app

        with TestClient(create_app()) as c:
            c.post("/auth/dev-login")
            assert 'href="/account/whatsapp"' not in c.get("/account").text
    finally:
        get_settings.cache_clear()


def test_the_account_page_offers_whatsapp_when_the_channel_is_on(wa):
    client, _ = wa
    body = client.get("/account").text
    assert 'href="/account/whatsapp"' in body
    assert "not linked" in body


def test_a_dead_waha_is_reported_not_crashed(monkeypatch, wa):
    client, fake = wa
    client.post("/account/whatsapp/acknowledge")

    def boom(name):
        raise waha.WahaTimeout("WAHA is down")

    monkeypatch.setattr(fake, "ensure_session", boom)
    body = client.post("/account/whatsapp/link", follow_redirects=True).text
    assert "WAHA is down" in body


def test_the_qr_is_404_when_there_is_no_session(wa):
    client, _ = wa
    assert client.get("/account/whatsapp/qr.png").status_code == 404


def test_signed_out_visitors_are_sent_home(wa):
    client, _ = wa
    client.post("/auth/logout")
    r = client.get("/account/whatsapp", follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/account/whatsapp/qr.png").status_code == 404


# --- the send pre-flight ------------------------------------------------------

def test_sendable_reads_the_live_session_not_our_cache(wa):
    """A pairing can die between a page load and a send, so the cached status must
    never be what authorises a send."""
    client, fake = wa
    _acked_and_linking(client)
    fake.status = waha.STATUS_WORKING
    client.get("/account/whatsapp")           # caches WORKING
    db, user = _user()
    assert user.wa_status == waha.STATUS_WORKING

    fake.status = waha.STATUS_FAILED          # ...but WhatsApp dropped it
    with pytest.raises(waha.NotLinked):
        link.sendable(db, user, get_settings())
    assert user.wa_status == waha.STATUS_FAILED  # and the cache is corrected


def test_sendable_refuses_when_timelocked(wa):
    client, fake = wa
    _acked_and_linking(client)
    fake.status = waha.STATUS_WORKING
    fake.timelock = waha.Timelock.parse(
        {"isActive": True, "timeEnforcementEnds": 1784477333, "enforcementType": "DEFAULT"}
    )
    db, user = _user()
    with pytest.raises(waha.Timelocked):
        link.sendable(db, user, get_settings())


def test_sendable_refuses_with_no_link_at_all(wa):
    client, _ = wa
    db, user = _user()
    with pytest.raises(waha.NotLinked):
        link.sendable(db, user, get_settings())


# --- pairing by typed code (for when you can't scan) --------------------------

def test_the_qr_screen_offers_the_by_number_path(wa):
    """A QR has to be scanned *by* the phone, so it's useless *on* the phone.
    The way out has to be visible without hunting for it."""
    client, fake = wa
    _acked_and_linking(client)
    body = client.get("/account/whatsapp").text
    assert 'action="/account/whatsapp/pairing-code"' in body
    assert 'name="phone"' in body
    assert "phone you" in body           # "...the phone you're linking?"


def test_requesting_a_code_shows_it_with_where_to_type_it(wa):
    client, fake = wa
    _acked_and_linking(client)
    r = client.post("/account/whatsapp/pairing-code", data={"phone": "+1 555 123 4567"})
    assert r.status_code == 200
    assert "WW5J-87T4" in r.text
    assert "phone number instead" in r.text          # where to type it
    assert ("code", "utest", "+15551234567") in fake.calls   # normalised first


def test_the_code_screen_hides_the_qr_so_there_is_one_thing_to_do(wa):
    client, fake = wa
    _acked_and_linking(client)
    body = client.post("/account/whatsapp/pairing-code", data={"phone": "+15551234567"}).text
    # No QR image on this screen (the shared poller script still names the URL,
    # but with no <img id="waQr"> to update it never fires).
    assert 'id="waQr"' not in body
    assert 'src="/account/whatsapp/qr.png"' not in body
    assert "Use a QR code instead" in body   # ...but the way back is there


def test_the_code_screen_still_polls_for_success(wa):
    """Typing the code is the last thing the host does, so the page must notice on
    its own. Safe because the session stays SCAN_QR_CODE while a code is
    outstanding, so the poller can't reload and wipe the code away."""
    client, fake = wa
    _acked_and_linking(client)
    body = client.post("/account/whatsapp/pairing-code", data={"phone": "+15551234567"}).text
    assert "/account/whatsapp/state" in body
    assert "check now" in body               # and a no-JS way to do the same


def test_a_number_without_a_country_code_is_refused_kindly(wa):
    client, fake = wa
    _acked_and_linking(client)
    r = client.post("/account/whatsapp/pairing-code", data={"phone": "555 123 4567"})
    assert "country code" in r.text
    assert not any(c[0] == "code" for c in fake.calls), "never guess the country"
    assert "555 123 4567" in r.text  # what they typed survives, to fix not retype


def test_the_number_is_used_once_and_not_stored(wa):
    client, fake = wa
    _acked_and_linking(client)
    client.post("/account/whatsapp/pairing-code", data={"phone": "+15551234567"})
    db, user = _user()
    # Only a completed pairing sets the linked number, and that comes from WAHA.
    assert user.wa_number is None


def test_a_refused_code_request_is_reported_not_crashed(wa):
    client, fake = wa
    _acked_and_linking(client)
    fake.code_error = waha.WahaError("WhatsApp refused the pairing request")
    r = client.post("/account/whatsapp/pairing-code", data={"phone": "+15551234567"})
    assert r.status_code == 200 and "WhatsApp refused" in r.text


def test_a_new_code_can_be_requested_for_the_same_number(wa):
    client, fake = wa
    _acked_and_linking(client)
    client.post("/account/whatsapp/pairing-code", data={"phone": "+15551234567"})
    body = client.post("/account/whatsapp/pairing-code", data={"phone": "+15551234567"}).text
    assert "WW5J-87T4" in body
    assert len([c for c in fake.calls if c[0] == "code"]) == 2


def test_pairing_by_code_completes_like_any_other(wa):
    client, fake = wa
    _acked_and_linking(client)
    client.post("/account/whatsapp/pairing-code", data={"phone": "+15551234567"})
    fake.status, fake.phone = waha.STATUS_WORKING, "+15551234567"
    body = client.get("/account/whatsapp").text
    assert "+15551234567" in body
    db, user = _user()
    assert user.wa_status == waha.STATUS_WORKING and user.wa_linked_at is not None


def test_the_code_route_needs_sign_in(wa):
    client, _ = wa
    client.post("/auth/logout")
    r = client.post("/account/whatsapp/pairing-code", data={"phone": "+15551234567"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_asking_for_a_code_on_a_dead_session_says_so_plainly(wa):
    """WAHA only issues codes in SCAN_QR_CODE and answers 422 otherwise, with a
    message written for developers. An unpaired session drifts to FAILED by
    itself, so this is a normal thing to walk into."""
    client, fake = wa
    _acked_and_linking(client)
    fake.status = waha.STATUS_FAILED
    r = client.post("/account/whatsapp/pairing-code", data={"phone": "+15551234567"})
    assert "expired" in r.text and "Link WhatsApp" in r.text
    assert not any(c[0] == "code" for c in fake.calls), "don't ask when it can't answer"



def test_pressing_link_recovers_a_failed_session(wa):
    """The bug a real host hit: Link returned 303 and changed nothing, leaving
    them stuck on "that pairing attempt didn't finish" forever. A FAILED session
    needs restart; start is a no-op on it."""
    client, fake = wa
    _acked_and_linking(client)
    fake.status = waha.STATUS_FAILED
    assert "pairing attempt" in client.get("/account/whatsapp").text

    client.post("/account/whatsapp/link", follow_redirects=False)
    # The session name is derived from the user id, so match on the verb.
    assert [c[0] for c in fake.calls].count("restart") == 1
    body = client.get("/account/whatsapp").text
    assert "pairing attempt" not in body, "still stranded after pressing Link"
    assert "/account/whatsapp/qr.png" in body
    assert 'name="phone"' in body       # ...and the by-number path is back too


def test_a_success_never_renders_under_a_stale_error(wa):
    """The contradiction a real host saw: "That pairing attempt has expired"
    sitting directly above "Linked as +1...".

    The page used to re-read the session after the caller had already read it, so
    an error derived from the first read could be rendered beside a newer state
    that disproved it — exactly what happens when the pairing completes between
    the two reads.
    """
    client, fake = wa
    _acked_and_linking(client)
    # Pairing finishes; the host's browser then submits the number form anyway.
    fake.status, fake.phone = waha.STATUS_WORKING, "+15550009999"
    r = client.post("/account/whatsapp/pairing-code", data={"phone": "+15550009999"})
    assert r.status_code == 200
    assert "+15550009999" in r.text          # the linked state
    assert "expired" not in r.text           # ...and no stale complaint above it
    # The class name lives in the inlined stylesheet; the element is the tell.
    assert '<p class="form-error">' not in r.text


def test_a_linked_page_never_shows_a_leftover_code(wa):
    client, fake = wa
    _acked_and_linking(client)
    body = client.post("/account/whatsapp/pairing-code", data={"phone": "+15550009999"}).text
    assert "WW5J-87T4" in body               # code while pairing
    fake.status, fake.phone = waha.STATUS_WORKING, "+15550009999"
    body = client.post("/account/whatsapp/pairing-code", data={"phone": "+15550009999"}).text
    assert "WW5J-87T4" not in body           # ...moot once linked
    assert "Unlink WhatsApp" in body


def test_the_page_reads_the_session_once_per_request(wa):
    """Two reads per request is how the states diverged in the first place."""
    client, fake = wa
    _acked_and_linking(client)
    before = len([c for c in fake.calls if c[0] == "find"])
    client.post("/account/whatsapp/pairing-code", data={"phone": "+15550009999"})
    assert len([c for c in fake.calls if c[0] == "find"]) - before == 1
