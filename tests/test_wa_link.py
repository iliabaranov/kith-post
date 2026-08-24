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
        return self._state()

    def qr_png(self, name):
        self.calls.append(("qr", name))
        return b"\x89PNG\r\n\x1a\nfake-qr"

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
