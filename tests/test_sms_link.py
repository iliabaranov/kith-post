"""A host's own texting setup: /account/sms, and what the rest of the app does with it.

SMS started out as one provider for the whole box. That is fine until a second
host wants their invitations to come from their own number, so a host can now
put their phone or their Twilio account on this page and everything downstream
— the send path, the reminder sweep, the compose form, the webhooks — resolves
*their* settings instead of the operator's.

Three properties carry the weight here:

* **Precedence is unambiguous.** A complete host row wins over the site's
  settings; an incomplete one falls through to nothing rather than to the
  operator's number, because a host halfway through setting up their own texting
  would be astonished to find their cards leaving from someone else's SIM.
* **Secrets go in and never come out.** The gateway password and the Twilio auth
  token are encrypted at rest, are never echoed into the form (blank means
  "keep"), and never appear in a response body or an export. The one secret the
  page *does* show is the webhook signing key, which exists to be typed into the
  phone.
* **One host's phone can only speak for that host.** Each gateway gets its own
  webhook URL and its own signing key, and a receipt is matched only against the
  host it was verified for — otherwise a shared Twilio account or a captured
  POST would let one host stamp deliveries onto another's guests.

Nothing here touches the network: providers are either faked outright or built
for real over an httpx.MockTransport.
"""

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from kith.config import get_settings
from kith.db.models import Event, Recipient, SmsLink, SmsOptOutEvent, User
from kith.services import sms, sms_link
from kith.services import sms_twilio as twilio
from kith.services.contacts import phone_hash

GATEWAY_URL = "http://192.168.1.50:8080"
GATEWAY_USER, GATEWAY_PASS = "sms", "gateway-secret-pw"
HOST_SIM = "+15550007777"
HOST_MOBILE = "+15550008888"
GUEST = "+15551110000"

# The site's own Twilio settings, when a test needs a site to override.
SITE_SID, SITE_TOKEN = "AC_site_0123456789", "site-auth-token"
SITE_FROM = "+15550001234"

HOST_SID, HOST_TOKEN = "AC_host_0123456789", "host-auth-token"
HOST_TWILIO_FROM = "+15550002345"


# --- fixtures ------------------------------------------------------------------

def _fresh_client(monkeypatch, **env):
    """A signed-in host on an app built from exactly this environment.

    Not the shared `client` fixture: several routes close over a settings
    snapshot taken when the app was built, so asserting on "the site provides
    texting" or "it doesn't" needs an app built after the environment is set.
    """
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    from kith.web.app import create_app

    c = TestClient(create_app())
    c.__enter__()
    c.post("/auth/dev-login")
    db, user = _db_and_user()
    user.display_name = "Ilia"
    db.commit()
    return c


SITE_OFF = {
    "KITH_SMS_ENABLED": "false", "KITH_SMS_PROVIDER": "none",
    "KITH_SMS_TWILIO_ACCOUNT_SID": "", "KITH_SMS_TWILIO_AUTH_TOKEN": "",
    "KITH_SMS_TWILIO_FROM": "", "KITH_SMS_SELF_NUMBER": "",
    "KITH_SMS_GATEWAY_URL": "", "KITH_SMS_GATEWAY_USER": "", "KITH_SMS_GATEWAY_PASS": "",
    "KITH_SMS_WEBHOOK_SECRET": "",
}

SITE_TWILIO = {
    **SITE_OFF,
    "KITH_SMS_ENABLED": "true", "KITH_SMS_PROVIDER": "twilio",
    "KITH_SMS_TWILIO_ACCOUNT_SID": SITE_SID, "KITH_SMS_TWILIO_AUTH_TOKEN": SITE_TOKEN,
    "KITH_SMS_TWILIO_FROM": SITE_FROM, "KITH_SMS_SELF_NUMBER": "+15550009999",
    "KITH_SMS_WEBHOOK_SECRET": "site-webhook-secret",
}


@pytest.fixture
def host(monkeypatch):
    """The interesting case: the operator configured nothing, the host will."""
    c = _fresh_client(monkeypatch, **SITE_OFF)
    try:
        yield c
    finally:
        c.__exit__(None, None, None)
        get_settings.cache_clear()


@pytest.fixture
def host_over_site(monkeypatch):
    """The site provides texting *and* the host has their own settings."""
    c = _fresh_client(monkeypatch, **SITE_TWILIO)
    try:
        yield c
    finally:
        c.__exit__(None, None, None)
        get_settings.cache_clear()


def _db():
    from kith.db.session import make_engine, make_session_factory

    return make_session_factory(make_engine(get_settings().db_path))()


def _db_and_user():
    db = _db()
    return db, db.execute(select(User)).scalars().first()


def _link():
    db = _db()
    return db, db.execute(select(SmsLink)).scalars().first()


# --- form payloads -------------------------------------------------------------

def _gateway_form(**over):
    form = {
        "provider": "gateway", "gateway_url": GATEWAY_URL,
        "gateway_user": GATEWAY_USER, "gateway_pass": GATEWAY_PASS,
        "sender_number": HOST_SIM, "self_number": HOST_MOBILE,
    }
    form.update(over)
    return form


def _twilio_form(**over):
    form = {
        "provider": "twilio", "twilio_account_sid": HOST_SID,
        "twilio_auth_token": HOST_TOKEN, "twilio_from": HOST_TWILIO_FROM,
        "self_number": HOST_MOBILE,
    }
    form.update(over)
    return form


def _save(client, form):
    return client.post("/account/sms", data=form, follow_redirects=False)


def _save_gateway(client, **over):
    r = _save(client, _gateway_form(**over))
    assert r.status_code == 303, "the fixture's own save should succeed"
    return r


# --- 1. which settings a host's texts use --------------------------------------

def test_no_row_and_a_site_that_texts_nothing_resolves_to_nothing(host):
    """Neither side configured is not "half configured": it is off."""
    db, user = _db_and_user()
    assert sms_link.config_for(db, user, get_settings()) is None
    assert sms_link.configured_for(db, user, get_settings()) is False


def test_no_row_falls_through_to_the_sites_settings(host_over_site):
    """The operator's configuration keeps working exactly as it did before
    hosts could have their own — that is the whole compatibility promise."""
    db, user = _db_and_user()
    cfg = sms_link.config_for(db, user, get_settings())
    assert cfg is not None and cfg.configured
    assert (cfg.source, cfg.provider) == ("site", "twilio")
    assert cfg.twilio_account_sid == SITE_SID


def test_a_complete_host_row_wins_over_a_configured_site(host_over_site):
    """Whose number the guest sees is the host's decision, not the operator's."""
    _save_gateway(host_over_site)
    db, user = _db_and_user()
    cfg = sms_link.config_for(db, user, get_settings())
    assert (cfg.source, cfg.provider) == ("host", "gateway")
    assert cfg.gateway_url == GATEWAY_URL
    assert cfg.gateway_pass == GATEWAY_PASS
    assert cfg.self_number == HOST_MOBILE


def test_an_incomplete_host_row_resolves_to_nothing_not_to_the_site(host_over_site):
    """A host mid-setup must not have their cards go out from the site's number.

    The row is written straight to the database rather than through the form,
    because the form refuses to save a half-filled one — but a provider switch,
    a partial migration or a hand-edited row can all produce this, and the
    resolution has to be "nothing", not "someone else's Twilio".
    """
    db, user = _db_and_user()
    db.add(SmsLink(
        user_id=user.id, provider="twilio", twilio_account_sid=HOST_SID,
        twilio_auth_token=HOST_TOKEN, webhook_token="tok-partial",
    ))
    db.commit()
    assert sms_link.config_for(db, user, get_settings()) is None
    assert sms_link.configured_for(db, user, get_settings()) is False


def test_turning_host_links_off_leaves_only_the_sites_settings(monkeypatch):
    """An operator who does not want hosts holding credentials can say so, and
    a row already saved stops being consulted rather than being deleted."""
    c = _fresh_client(monkeypatch, **SITE_TWILIO)
    try:
        _save_gateway(c)
    finally:
        c.__exit__(None, None, None)
    c = _fresh_client(
        monkeypatch, **SITE_TWILIO, KITH_SMS_HOST_LINKS_ENABLED="false",
    )
    try:
        db, user = _db_and_user()
        cfg = sms_link.config_for(db, user, get_settings())
        assert (cfg.source, cfg.provider) == ("site", "twilio")
        assert sms_link.available(get_settings()) is False
        r = c.get("/account/sms", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/account"
        assert "/account/sms" not in c.get("/account").text
    finally:
        c.__exit__(None, None, None)
        get_settings.cache_clear()


# --- 2. the page itself ---------------------------------------------------------

def test_the_page_needs_a_session(host):
    """Someone else's provider credentials are nobody's business."""
    host.post("/auth/logout")
    r = host.get("/account/sms", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_the_signed_in_page_offers_the_form(host):
    r = host.get("/account/sms")
    assert r.status_code == 200
    assert "Send invitations by text" in r.text
    assert 'name="gateway_url"' in r.text and 'name="twilio_account_sid"' in r.text


def test_the_account_page_points_at_it(host):
    assert "/account/sms" in host.get("/account").text


# --- 3. saving, and what is kept where -----------------------------------------

def test_a_saved_gateway_stores_its_password_encrypted(host):
    """The column is read back raw, past the ORM's decryption: a database file
    lifted off the box must not hand over the phone's password."""
    _save_gateway(host)
    db, link = _link()
    assert link.gateway_pass == GATEWAY_PASS          # the ORM decrypts it
    stored = db.execute(text("select gateway_pass from sms_links")).scalar_one()
    assert stored != GATEWAY_PASS
    assert GATEWAY_PASS not in stored


def test_saving_generates_a_webhook_token_and_signing_key(host):
    """Both are ours to generate: the token routes the phone's POSTs to this
    host, and the key is what proves they came from that phone."""
    _save_gateway(host)
    _db, link = _link()
    assert link.webhook_token and len(link.webhook_token) > 20
    assert link.webhook_secret and len(link.webhook_secret) > 20
    assert link.webhook_secret != link.webhook_token


def test_the_signing_key_is_shown_but_the_password_never_is(host):
    """The key exists to be typed into the app, so it has to be on the page.
    The password went the other way and has no reason to come back — not on the
    settings page, and not in the error round-trip of a failed re-save."""
    _save_gateway(host)
    _db, link = _link()
    body = host.get("/account/sms").text
    assert link.webhook_secret in body
    assert GATEWAY_PASS not in body

    failed = _save(host, _gateway_form(gateway_url="192.168.1.50:8080"))
    assert failed.status_code == 200
    assert GATEWAY_PASS not in failed.text


def test_a_twilio_token_never_comes_back_out_of_the_page(host):
    _save(host, _twilio_form())
    body = host.get("/account/sms").text
    assert HOST_SID in body                     # not a secret; it is in every URL
    assert HOST_TOKEN not in body
    failed = _save(host, _twilio_form(twilio_account_sid="XX1234567890"))
    assert HOST_TOKEN not in failed.text


# --- 4. what the form refuses, and what it keeps while refusing ----------------

@pytest.mark.parametrize(("over", "fragment"), [
    ({"gateway_url": "192.168.1.50:8080"}, "start with http://"),
    ({"gateway_url": GATEWAY_URL + "/message"}, "Leave the path off"),
    ({"self_number": "call me maybe"}, "country code"),
    ({"gateway_pass": ""}, "password the app shows"),
])
def test_a_bad_gateway_form_comes_back_with_the_error_and_the_typing(host, over, fragment):
    """Re-rendered rather than redirected, so nobody retypes an address they
    already got right — and the error says which field, in the host's terms."""
    r = _save(host, _gateway_form(**over))
    assert r.status_code == 200
    assert "form-error" in r.text and fragment in r.text
    assert GATEWAY_USER in r.text                # what they typed is still there
    _db, link = _link()
    assert link is None, "nothing half-valid should have been stored"


@pytest.mark.parametrize(("over", "fragment"), [
    ({"twilio_account_sid": "XX1234567890"}, "starts with AC"),
    ({"twilio_messaging_service_sid": "XX999"}, "starts with MG"),
    ({"twilio_auth_token": ""}, "Auth Token"),
    ({"twilio_from": "not a number"}, "country code"),
])
def test_a_bad_twilio_form_comes_back_with_the_error(host, over, fragment):
    r = _save(host, _twilio_form(**over))
    assert r.status_code == 200
    assert "form-error" in r.text and fragment in r.text
    _db, link = _link()
    assert link is None


def test_a_blank_password_on_a_later_save_keeps_the_stored_one(host):
    """The field is rendered empty because the password is never echoed back, so
    an empty field has to mean "unchanged" — otherwise every edit to the address
    would silently wipe the credential the host cannot read off this page."""
    _save_gateway(host)
    r = _save(host, _gateway_form(gateway_pass="", gateway_url="http://192.168.1.60:8080"))
    assert r.status_code == 303
    _db, link = _link()
    assert link.gateway_url == "http://192.168.1.60:8080"
    assert link.gateway_pass == GATEWAY_PASS


# --- 5. one way of texting at a time -------------------------------------------

def test_switching_to_twilio_clears_the_phone_and_forgets_the_old_test(host):
    """A row that kept both halves would leave a stale phone password lying
    about, and a "last test went through" from the provider they just left."""
    _save_gateway(host)
    db, link = _link()
    link.webhooks_registered_at = datetime.now(UTC)
    link.last_test_at = link.last_ok_at = datetime.now(UTC)
    link.last_error = "something old"
    db.commit()

    assert _save(host, _twilio_form()).status_code == 303
    _db2, link = _link()
    assert link.provider == "twilio"
    assert link.gateway_url is None and link.gateway_user is None
    assert link.gateway_pass is None and link.gateway_device_id is None
    assert link.webhooks_registered_at is None
    assert link.last_test_at is None and link.last_ok_at is None
    assert link.last_error is None


# --- 6. the test send -----------------------------------------------------------

class _FakeProvider:
    """Records what it was asked to send, and with which configuration."""

    def __init__(self, error=None):
        self.sent: list[tuple[str, str]] = []
        self.configs: list[sms.SmsConfig] = []
        self.error = error

    def send(self, to_e164, text_):
        if self.error is not None:
            raise self.error
        self.sent.append((to_e164, text_))
        return sms.SmsResult(message_id=f"m{len(self.sent)}")

    def capabilities(self):
        return sms.SmsCaps()


def _fake_provider(monkeypatch, fake):
    """Stand in for whatever provider the resolved configuration asked for."""
    def build(config):
        fake.configs.append(config)
        return fake

    monkeypatch.setattr(sms, "provider_from", build)
    return fake


def test_a_test_send_goes_to_the_hosts_own_mobile_and_is_recorded(host, monkeypatch):
    """A password is only ever proved by using it, and the one number it is safe
    to prove it on is the host's own."""
    _save_gateway(host)
    fake = _fake_provider(monkeypatch, _FakeProvider())
    r = host.post("/account/sms/test", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account/sms?notice=tested"
    assert [to for to, _ in fake.sent] == [HOST_MOBILE]
    _db, link = _link()
    assert link.last_ok_at is not None and link.last_error is None
    assert "Test text sent" in host.get("/account/sms?notice=tested").text


def test_a_test_send_still_goes_only_to_the_host_in_live_mode(host, monkeypatch):
    """Send mode governs guests. A credentials check has no guest in it, so it
    goes to the host's own number in every mode — there is nowhere else safe."""
    _save_gateway(host)
    monkeypatch.setenv("KITH_SEND_MODE", "live")
    get_settings.cache_clear()
    fake = _fake_provider(monkeypatch, _FakeProvider())
    host.post("/account/sms/test", follow_redirects=False)
    assert [to for to, _ in fake.sent] == [HOST_MOBILE]


def test_a_refused_password_is_explained_and_remembered(host, monkeypatch):
    """The host cannot see the stored password, so the page has to be the place
    that says it was refused — and to keep saying so on the next load."""
    _save_gateway(host)
    _fake_provider(monkeypatch, _FakeProvider(error=sms.SmsAuthError("nope")))
    r = host.post("/account/sms/test", follow_redirects=False)
    assert r.status_code == 200
    assert "username or password was refused" in r.text
    _db, link = _link()
    assert link.last_ok_at is None
    assert "refused" in link.last_error
    assert "username or password was refused" in host.get("/account/sms").text


def test_a_test_send_with_no_mobile_says_so_without_texting_anyone(host, monkeypatch):
    """There is no default destination to fall back on: a test text to a number
    the host did not name is a text to a stranger."""
    _save_gateway(host, self_number="")
    fake = _fake_provider(monkeypatch, _FakeProvider())
    r = host.post("/account/sms/test", follow_redirects=False)
    assert r.status_code == 200
    assert "Add your own mobile number" in r.text
    assert fake.sent == [] and fake.configs == []


# --- 7. telling the phone where to report --------------------------------------

def _webhook_recorder(monkeypatch, *, status=200):
    """A real AndroidGatewayProvider over a mock transport, recording its POSTs."""
    posted: list[httpx.Request] = []

    def handler(request):
        posted.append(request)
        return httpx.Response(status, json={"id": "wh"})

    def build(config):
        from kith.services.sms_gateway import AndroidGatewayProvider

        return AndroidGatewayProvider(
            config.gateway_url, config.gateway_user, config.gateway_pass,
            path=config.gateway_path, device_id=config.gateway_device_id,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(sms, "provider_from", build)
    return posted


def test_registering_webhooks_points_every_event_at_this_hosts_url(host, monkeypatch):
    """Four events, four registrations — a phone that reports deliveries but not
    STOP replies is a channel that honours receipts and not people. Each points
    at this host's own URL, which is how the receipt is later scoped to them."""
    _save_gateway(host)
    posted = _webhook_recorder(monkeypatch)
    r = host.post("/account/sms/webhooks", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account/sms?notice=webhooks"

    _db, link = _link()
    expected_url = f"{get_settings().base_url.rstrip('/')}/sms/webhook/gateway/{link.webhook_token}"
    assert [str(p.url) for p in posted] == [f"{GATEWAY_URL}/webhooks"] * 4
    bodies = [json.loads(p.content) for p in posted]
    assert [b["id"] for b in bodies] == [
        "kith-sms-received", "kith-sms-delivered", "kith-sms-failed", "kith-sms-cancelled",
    ]
    assert [b["event"] for b in bodies] == list(sms_link.GATEWAY_EVENTS)
    assert {b["url"] for b in bodies} == {expected_url}
    assert link.webhooks_registered_at is not None


def test_a_relay_registers_on_the_relays_own_path(host, monkeypatch):
    """The relay and the on-device server are the same provider at two paths;
    getting it wrong is a 404, which is why the path follows the send path's."""
    _save_gateway(host, gateway_path_relay="1")
    posted = _webhook_recorder(monkeypatch)
    assert host.post("/account/sms/webhooks", follow_redirects=False).status_code == 303
    assert [str(p.url) for p in posted] == [f"{GATEWAY_URL}/3rdparty/v1/webhooks"] * 4


def test_a_refused_registration_is_reported_and_not_marked_done(host, monkeypatch):
    _save_gateway(host)
    _webhook_recorder(monkeypatch, status=401)
    r = host.post("/account/sms/webhooks", follow_redirects=False)
    assert r.status_code == 200
    assert "username or password was refused" in r.text
    _db, link = _link()
    assert link.webhooks_registered_at is None


# --- 8. the send path uses the host's settings ---------------------------------

def _make_event(client, *, sms_to=GUEST, title="Joe's 3rd Birthday"):
    r = client.post(
        "/events",
        data={"title": title, "event_date": "2099-06-14", "event_time": "15:00",
              "recipients": "", "wa_recipients": "", "sms_recipients": sms_to,
              "block_rsvp": "on", "block_date": "on"},
        follow_redirects=False,
    )
    return r.headers["location"].split("/events/")[1].split("?")[0]


def _send(event_id):
    from kith.services import send as sender

    db, user = _db_and_user()
    return db, sender.send_event(db, db.get(Event, event_id), user, get_settings())


def _recipients(db, event_id):
    return db.execute(
        select(Recipient).where(Recipient.event_id == event_id)
    ).scalars().all()


def test_a_host_row_switches_the_channel_on_for_a_site_that_has_none(host, monkeypatch):
    """The whole point of the page: an operator who configured nothing still has
    hosts who can text, and the compose form has to offer the box to say so."""
    assert 'name="sms_recipients"' not in host.get("/events/new").text
    _save_gateway(host)
    assert 'name="sms_recipients"' in host.get("/events/new").text

    ev = _make_event(host)
    monkeypatch.setenv("KITH_SEND_MODE", "live")
    get_settings.cache_clear()
    fake = _fake_provider(monkeypatch, _FakeProvider())
    db, res = _send(ev)
    assert (res.sms_sent, res.sms_failed, res.sms_blocked) == (1, 0, None)
    assert [to for to, _ in fake.sent] == [GUEST]
    assert all(r.status == "sent" for r in _recipients(db, ev))


def test_the_send_path_is_handed_the_hosts_config_not_the_sites(
    host_over_site, monkeypatch
):
    """Both are configured, so this is the case where getting precedence wrong
    is invisible: the texts still go out, from the wrong number."""
    _save_gateway(host_over_site)
    ev = _make_event(host_over_site)
    monkeypatch.setenv("KITH_SEND_MODE", "live")
    get_settings.cache_clear()
    fake = _fake_provider(monkeypatch, _FakeProvider())
    _db, res = _send(ev)
    assert res.sms_sent == 1
    config = fake.configs[0]
    assert (config.source, config.provider) == ("host", "gateway")
    assert config.gateway_url == GATEWAY_URL
    assert config.twilio_account_sid == ""      # nothing of the site's leaked in


# --- 9. self-only is the host's own number, not the operator's -----------------

def test_self_only_texts_the_hosts_mobile_not_the_sites(host_over_site, monkeypatch):
    """The site has a test number of its own, and it is the wrong one: a host
    checking their card should get the text on their phone, not the operator's."""
    _save_gateway(host_over_site)
    ev = _make_event(host_over_site)
    monkeypatch.setenv("KITH_SEND_MODE", "self-only")
    get_settings.cache_clear()
    assert get_settings().sms_self_number == "+15550009999"     # the site's
    fake = _fake_provider(monkeypatch, _FakeProvider())
    _db, res = _send(ev)
    assert res.sms_sent == 1
    assert [to for to, _ in fake.sent] == [HOST_MOBILE]


# --- 10. reminders resolve the same way ----------------------------------------

def _sent_by_the_route(client, event_id):
    """Drive the real send route: the paced background batch is what schedules
    the nudges, so ``send_event`` alone leaves nothing for the sweep to find."""
    from kith.services import send as sender

    client.post(f"/events/{event_id}/send", follow_redirects=False)
    assert sender.wait_for_batches(timeout=30)


def _due_reminder(event_id):
    from kith.db.models import Reminder

    db = _db()
    rem = db.execute(
        select(Reminder).where(Reminder.event_id == event_id)
    ).scalars().first()
    assert rem is not None, "the dry-run send should have planned a nudge"
    rem.scheduled_for = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    return db, rem


def test_a_reminder_goes_out_through_the_hosts_own_provider(host, monkeypatch):
    """A nudge sent weeks later must resolve the channel afresh — the host may
    have set theirs up after the card went out, or changed it since."""
    from kith.services import scheduler

    _save_gateway(host)
    ev = _make_event(host)
    _sent_by_the_route(host, ev)                 # dry-run: plans the reminders
    db, rem = _due_reminder(ev)

    monkeypatch.setenv("KITH_SEND_MODE", "live")
    get_settings.cache_clear()
    fake = _fake_provider(monkeypatch, _FakeProvider())
    assert scheduler.send_one_reminder(db, rem, get_settings()) is True
    assert rem.status == "sent"
    assert [to for to, _ in fake.sent] == [GUEST]
    assert fake.configs[0].source == "host"


def test_a_reminder_is_skipped_when_the_host_removes_their_setup(host, monkeypatch):
    """Held reminders are retried every tick for ever, and nothing bounds that
    while the channel stays off — so a removed setup skips rather than holds."""
    from kith.services import scheduler

    _save_gateway(host)
    ev = _make_event(host)
    _sent_by_the_route(host, ev)
    db, rem = _due_reminder(ev)

    assert host.post("/account/sms/remove", follow_redirects=False).status_code == 303
    monkeypatch.setenv("KITH_SEND_MODE", "live")
    get_settings.cache_clear()
    assert scheduler.send_one_reminder(db, rem, get_settings()) is False
    assert (rem.status, rem.skip_reason) == ("skipped", "channel_off")


# --- 11. the per-host gateway webhook ------------------------------------------

def _second_host(*, sms_message_id="msg-theirs"):
    """Another host with their own gateway row, card and texted guest."""
    db = _db()
    other = User(google_sub="other-host", email="other@example.com", display_name="Bo")
    db.add(other)
    db.flush()
    link = sms_link.save(
        db, other, provider="gateway", gateway_url="http://192.168.1.99:8080",
        gateway_user="bo", gateway_pass="bo-pw", sender_number="+15550006666",
        self_number="+15550005555",
    )
    ev = Event(user_id=other.id, title="Bo's housewarming", blocks={"rsvp": True})
    db.add(ev)
    db.flush()
    r = Recipient(
        event_id=ev.id, channel="sms", email="", phone="+15559990000",
        name="Guest", status="sent", token="tok-other", sms_message_id=sms_message_id,
    )
    db.add(r)
    db.commit()
    return db, link, r


def _sent_recipient(client, *, message_id="msg-ours"):
    ev = _make_event(client)
    db = _db()
    row = db.execute(select(Recipient).where(Recipient.event_id == ev)).scalars().one()
    row.status, row.sms_message_id = "sent", message_id
    db.commit()
    return db, row


def _gateway_post(client, token, body, *, secret, ts=None):
    raw = json.dumps(body).encode()
    ts = str(int(time.time())) if ts is None else str(ts)
    signature = hmac.new(secret.encode(), raw + ts.encode(), hashlib.sha256).hexdigest()
    return client.post(
        f"/sms/webhook/gateway/{token}", content=raw,
        headers={
            "content-type": "application/json",
            sms.GATEWAY_SIGNATURE_HEADER: signature,
            sms.GATEWAY_TIMESTAMP_HEADER: ts,
        },
    )


def test_a_hosts_phone_reports_a_delivery_on_that_hosts_recipient(host):
    _save_gateway(host)
    db, row = _sent_recipient(host)
    _dbl, link = _link()
    r = _gateway_post(
        host, link.webhook_token,
        {"event": "sms:delivered", "payload": {"messageId": "msg-ours"}},
        secret=link.webhook_secret,
    )
    assert r.status_code == 200
    db.refresh(row)
    assert row.sms_delivered_at is not None


def test_one_hosts_signature_does_not_open_another_hosts_url(host):
    """The token says whose phone this claims to be; only that host's key proves
    it. Otherwise anyone with one valid gateway could report on every card."""
    _save_gateway(host)
    _dbl, mine = _link()
    db_other, theirs, _row = _second_host()
    r = _gateway_post(
        host, theirs.webhook_token,
        {"event": "sms:delivered", "payload": {"messageId": "msg-theirs"}},
        secret=mine.webhook_secret,
    )
    assert r.status_code == 401
    db_other.expire_all()
    assert db_other.get(SmsLink, theirs.user_id) is not None


def test_a_receipt_for_another_hosts_message_changes_nothing(host):
    """Two hosts can share a provider account, and message ids are only unique
    within one. Matching on the id alone would let one host's phone stamp a
    delivery onto a guest of the other's."""
    _save_gateway(host)
    _dbl, mine = _link()
    db_other, _theirs, other_row = _second_host(sms_message_id="msg-shared")
    r = _gateway_post(
        host, mine.webhook_token,
        {"event": "sms:delivered", "payload": {"messageId": "msg-shared"}},
        secret=mine.webhook_secret,
    )
    assert r.status_code == 200 and r.json() == {"status": "not one of ours"}
    db_other.refresh(other_row)
    assert other_row.sms_delivered_at is None


def test_a_stop_without_a_country_code_is_resolved_from_the_hosts_own_sim(host):
    """A handset reports a domestic sender as bare digits. Dropping the opt-out
    because of that would be the worst possible reading of "we don't guess
    countries" — so the country comes from the number the host told us is theirs."""
    _save_gateway(host)
    _dbl, link = _link()
    r = _gateway_post(
        host, link.webhook_token,
        {"event": "sms:received", "payload": {"sender": "6505551212", "message": "STOP"}},
        secret=link.webhook_secret,
    )
    assert r.status_code == 200 and r.json() == {"status": "stop"}
    db = _db()
    rows = db.execute(select(SmsOptOutEvent)).scalars().all()
    assert [x.kind for x in rows] == ["stop"]
    assert rows[0].phone_hash == phone_hash("+16505551212")


def test_an_unknown_token_is_not_an_endpoint(host):
    """An endpoint whose signature can never be satisfied is better off silent."""
    _save_gateway(host)
    r = host.post(
        "/sms/webhook/gateway/no-such-token", json={"event": "sms:delivered"},
    )
    assert r.status_code == 404


def test_the_site_wide_gateway_endpoint_stays_shut_for_a_hosts_phone(host):
    """The operator configured no gateway, so the site's own URL has no secret
    to verify with — a host's key must not unlock it by proxy."""
    _save_gateway(host)
    _dbl, link = _link()
    raw = json.dumps({"event": "sms:delivered", "payload": {"messageId": "msg-ours"}}).encode()
    ts = str(int(time.time()))
    sig = hmac.new(link.webhook_secret.encode(), raw + ts.encode(), hashlib.sha256).hexdigest()
    r = host.post(
        "/sms/webhook/gateway", content=raw,
        headers={
            "content-type": "application/json",
            sms.GATEWAY_SIGNATURE_HEADER: sig, sms.GATEWAY_TIMESTAMP_HEADER: ts,
        },
    )
    assert r.status_code == 404


# --- 12. the Twilio webhook, found by AccountSid --------------------------------

def _twilio_post(client, params, *, token):
    url = f"{get_settings().base_url}/sms/webhook/twilio"
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    import base64

    signature = base64.b64encode(
        hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()
    return client.post(
        "/sms/webhook/twilio", data=params,
        headers={twilio.TWILIO_SIGNATURE_HEADER: signature},
    )


def test_a_twilio_callback_finds_the_host_by_account_sid(host):
    """One URL serves every host's Twilio account, so the AccountSid in the POST
    is what says whose token to check the signature with."""
    assert _save(host, _twilio_form()).status_code == 303
    db, row = _sent_recipient(host, message_id="SM_host")
    r = _twilio_post(host, {
        "AccountSid": HOST_SID, "MessageSid": "SM_host", "MessageStatus": "delivered",
    }, token=HOST_TOKEN)
    assert r.status_code == 200
    db.refresh(row)
    assert row.sms_delivered_at is not None


def test_a_twilio_callback_signed_with_the_wrong_token_is_refused(host):
    _save(host, _twilio_form())
    db, row = _sent_recipient(host, message_id="SM_host")
    r = _twilio_post(host, {
        "AccountSid": HOST_SID, "MessageSid": "SM_host", "MessageStatus": "delivered",
    }, token="not-the-token")
    assert r.status_code == 401
    db.refresh(row)
    assert row.sms_delivered_at is None


def test_a_twilio_callback_for_an_account_nobody_here_uses_is_a_404(host):
    """No site Twilio and no host on that account means there is nobody to
    verify for, and an unverifiable endpoint should not answer at all."""
    _save(host, _twilio_form())
    r = _twilio_post(host, {
        "AccountSid": "AC_stranger", "MessageSid": "SM_x", "MessageStatus": "delivered",
    }, token=HOST_TOKEN)
    assert r.status_code == 404


# --- 13. export and delete ------------------------------------------------------

def test_the_export_describes_the_hosts_own_setup_without_its_secrets(host):
    """An export is a file that gets forwarded, so it is the wrong place for a
    password — but a host is entitled to know what they configured and when."""
    _save_gateway(host)
    _dbl, link = _link()
    data = host.get("/account/export").json()
    assert data["sms"]["configured"] is True
    assert data["sms"]["source"] == "host"
    own = data["sms"]["own_setup"]
    assert own["provider"] == "gateway"
    assert own["gateway_url"] == GATEWAY_URL
    assert own["sender_number"] == HOST_SIM and own["self_number"] == HOST_MOBILE
    assert own["created_at"] is not None

    dump = json.dumps(data)
    assert GATEWAY_PASS not in dump
    assert link.webhook_secret not in dump
    assert "webhook_secret" not in dump and "gateway_pass" not in dump


def test_the_export_carries_no_twilio_token_either(host):
    _save(host, _twilio_form())
    data = host.get("/account/export").json()
    assert data["sms"]["own_setup"]["twilio_account_sid"] == HOST_SID
    assert HOST_TOKEN not in json.dumps(data)


def test_deleting_the_account_takes_the_texting_setup_with_it(host):
    """Credentials for someone's phone must not outlive the account that holds
    them; the hashed opt-out log, which names nobody, still has to."""
    _save_gateway(host)
    db = _db()
    db.add(SmsOptOutEvent(phone_hash=phone_hash(GUEST), kind="stop", source="gateway"))
    db.commit()

    assert host.post("/account/delete", follow_redirects=False).status_code == 303
    db2 = _db()
    assert db2.execute(select(SmsLink)).scalars().all() == []
    assert len(db2.execute(select(SmsOptOutEvent)).scalars().all()) == 1


# --- 14. removing it ------------------------------------------------------------

def test_removing_the_setup_takes_the_channel_back_off(host):
    """Cards already sent keep working; what stops is the ability to send new
    texts — and the account page has to stop claiming otherwise."""
    _save_gateway(host)
    assert "Text messages: on" in host.get("/account").text
    r = host.post("/account/sms/remove", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account/sms?notice=removed"
    _db, link = _link()
    assert link is None
    assert "Text messages: not set up" in host.get("/account").text


# --- 15. the settings-only factory still works ---------------------------------

def test_get_provider_still_builds_the_sites_provider_from_settings_alone(monkeypatch):
    """Kept for callers reasoning about the operator's configuration by itself;
    a host's row must not be needed to answer "what did the operator set up?"."""
    from kith.config import Settings
    from kith.services.sms_gateway import AndroidGatewayProvider
    from kith.services.sms_twilio import TwilioProvider

    tw = Settings(
        sms_enabled=True, sms_provider="twilio", sms_twilio_account_sid=SITE_SID,
        sms_twilio_auth_token=SITE_TOKEN, sms_twilio_from=SITE_FROM,
    )
    assert isinstance(sms.get_provider(tw), TwilioProvider)
    gw = Settings(
        sms_enabled=True, sms_provider="gateway", sms_gateway_url=GATEWAY_URL,
        sms_gateway_user=GATEWAY_USER, sms_gateway_pass=GATEWAY_PASS,
    )
    assert isinstance(sms.get_provider(gw), AndroidGatewayProvider)
    assert isinstance(sms.get_provider(Settings(sms_enabled=False)), sms.NullProvider)
