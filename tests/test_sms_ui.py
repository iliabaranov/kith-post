"""The SMS channel's front end: compose box, contact picker, dashboard, preview.

The load-bearing property is that the UI tells the truth about what pressing
Send will do. A button that says "from your Gmail" over a card addressed
entirely by text, or an SMS box on an instance with no provider, is worse than
no SMS support at all.

SMS is instance-level, so there is deliberately no linking page and no
/account/sms route — those absences are asserted here too.
"""

import html
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kith.config import get_settings
from kith.db.models import Recipient, User
from kith.services import contacts as book


def _fresh_client(monkeypatch, **env):
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


@pytest.fixture
def sms_client(monkeypatch):
    """SMS on, WhatsApp off — so an assertion about the SMS box can't pass by
    accident on the WhatsApp one.

    The dummy Twilio credentials are what make sms_configured true; a named but
    uncredentialed provider is deliberately not configured. Nothing here sends,
    so no credential is ever used.
    """
    c = _fresh_client(
        monkeypatch, KITH_SMS_ENABLED="true", KITH_SMS_PROVIDER="twilio",
        KITH_SMS_TWILIO_ACCOUNT_SID="AC_test", KITH_SMS_TWILIO_AUTH_TOKEN="tok_test",
        KITH_SMS_TWILIO_FROM="+15550001234",
    )
    try:
        yield c
    finally:
        c.__exit__(None, None, None)
        get_settings.cache_clear()


@pytest.fixture
def both_client(monkeypatch):
    """Both phone channels on: the case where the host has to choose.

    WhatsApp also needs a live session before its box appears (wa_ready is "the
    channel is on AND this host has linked"), so the session is faked here — no
    WAHA call is made, since nothing in this module sends.
    """
    from kith.services import waha

    c = _fresh_client(
        monkeypatch, KITH_SMS_ENABLED="true", KITH_SMS_PROVIDER="twilio",
        KITH_SMS_TWILIO_ACCOUNT_SID="AC_test", KITH_SMS_TWILIO_AUTH_TOKEN="tok_test",
        KITH_SMS_TWILIO_FROM="+15550001234",
        KITH_WHATSAPP_ENABLED="true", KITH_WAHA_API_KEY="test-key",
    )
    db, user = _db_and_user()
    user.wa_session = "utest"
    user.wa_status = waha.STATUS_WORKING
    user.wa_number = "+15550009999"
    db.commit()
    try:
        yield c
    finally:
        c.__exit__(None, None, None)
        get_settings.cache_clear()


@pytest.fixture
def off_client(monkeypatch):
    """A fresh app with both phone channels explicitly off.

    Not the shared `client` fixture: /account renders from a settings snapshot
    captured when the app was built, so the module-level app carries whatever
    environment happened to be set the first time something imported it. A
    purpose-built app is the only way to assert on "the channel is off".
    """
    c = _fresh_client(
        monkeypatch, KITH_SMS_ENABLED="false", KITH_SMS_PROVIDER="none",
        KITH_SMS_TWILIO_ACCOUNT_SID="", KITH_SMS_TWILIO_AUTH_TOKEN="",
        KITH_SMS_TWILIO_FROM="",
        KITH_WHATSAPP_ENABLED="false", KITH_WAHA_API_KEY="",
    )
    try:
        yield c
    finally:
        c.__exit__(None, None, None)
        get_settings.cache_clear()


def _db_and_user():
    from kith.db.session import make_engine, make_session_factory

    db = make_session_factory(make_engine(get_settings().db_path))()
    return db, db.execute(select(User)).scalars().first()


def _make_event(client, *, sms_to="", emails="", wa_to=""):
    r = client.post(
        "/events",
        data={"title": "Joe's 3rd Birthday", "event_date": "2099-06-14",
              "event_time": "15:00", "recipients": emails,
              "wa_recipients": wa_to, "sms_recipients": sms_to,
              "block_rsvp": "on", "block_date": "on", "block_time": "on"},
        follow_redirects=False,
    )
    return r.headers["location"].split("/events/")[1].split("?")[0]


# --- the compose box ----------------------------------------------------------

def test_the_sms_box_appears_when_the_channel_is_configured(sms_client):
    body = sms_client.get("/events/new").text
    assert 'name="sms_recipients"' in body
    assert "Anyone by text?" in body
    assert "no card picture" in body          # honest about what a text can't do
    assert "country code" in body


def test_the_sms_box_is_absent_when_the_channel_is_off(off_client):
    body = off_client.get("/events/new").text
    assert 'name="sms_recipients"' not in body
    assert "Anyone by text?" not in body


def test_there_is_no_sms_linking_page(sms_client):
    """SMS is configured once for the site; a host has nothing to link."""
    assert sms_client.get("/account/sms", follow_redirects=False).status_code == 404
    assert "/account/sms" not in sms_client.get("/account").text


def test_the_account_page_states_the_channel_without_offering_a_manage_link(sms_client):
    body = sms_client.get("/account").text
    assert "Text messages: on" in body
    assert "/account/sms" not in body


def test_the_account_page_says_nothing_when_the_channel_is_off(off_client):
    assert "Text messages: on" not in off_client.get("/account").text


def test_the_box_is_repopulated_when_editing(sms_client):
    ev = _make_event(sms_client, sms_to="Mara <+15551110000>")
    body = sms_client.get(f"/events/{ev}/edit").text
    assert "+15551110000" in body
    # And in the SMS box, not the WhatsApp one — they both hold numbers.
    after = body.split('name="sms_recipients"', 1)[1]
    assert "+15551110000" in after.split("</textarea>", 1)[0]


def test_an_sms_guest_is_not_listed_in_the_whatsapp_box_when_editing(both_client):
    """Both channels store a number, so splitting on "has a phone" would list an
    SMS guest under WhatsApp — and move them there on the next save."""
    ev = _make_event(both_client, wa_to="+15551110000", sms_to="+15552220000")
    body = both_client.get(f"/events/{ev}/edit").text
    wa_box = body.split('name="wa_recipients"', 1)[1].split("</textarea>", 1)[0]
    sms_box = body.split('name="sms_recipients"', 1)[1].split("</textarea>", 1)[0]
    assert "+15551110000" in wa_box and "+15552220000" not in wa_box
    assert "+15552220000" in sms_box and "+15551110000" not in sms_box


# --- the contact picker -------------------------------------------------------

def test_a_phone_only_contact_gets_a_fixed_label_when_one_channel_is_on(sms_client):
    db, user = _db_and_user()
    book.add_contact(db, user.id, "", "Mara", phone="+15551110000")
    body = sms_client.get("/events/new").text
    assert 'class="book-ch-fixed">text<' in body
    assert 'class="book-ch"' not in body       # nothing to choose


def test_a_phone_only_contact_gets_a_picker_when_both_channels_are_on(both_client):
    db, user = _db_and_user()
    book.add_contact(db, user.id, "", "Mara", phone="+15551110000")
    body = both_client.get("/events/new").text
    assert 'class="book-ch"' in body
    select = body.split('class="book-ch"', 1)[1].split("</select>", 1)[0]
    assert '<option value="whatsapp">' in select
    assert '<option value="sms">' in select
    assert '<option value="email">' not in select    # they have no address


def test_a_contact_with_both_offers_all_enabled_channels(both_client):
    db, user = _db_and_user()
    book.add_contact(db, user.id, "mara@example.com", "Mara", phone="+15551110000")
    body = both_client.get("/events/new").text
    select = body.split('class="book-ch"', 1)[1].split("</select>", 1)[0]
    assert '<option value="email">' in select
    assert '<option value="whatsapp">' in select
    assert '<option value="sms">' in select


def test_the_picker_javascript_knows_about_the_third_box(sms_client):
    body = sms_client.get("/events/new").text
    assert 'getElementById("sms_recipients")' in body


# --- the dashboard ------------------------------------------------------------

def test_the_dashboard_badges_an_sms_recipient(sms_client):
    ev = _make_event(sms_client, sms_to="Mara <+15551110000>")
    body = sms_client.get(f"/events/{ev}").text
    assert "· SMS" in body
    assert "· WhatsApp" not in body


def test_an_unnamed_sms_recipient_is_still_badged(sms_client):
    ev = _make_event(sms_client, sms_to="+15551110000")
    assert "· SMS" in sms_client.get(f"/events/{ev}").text


def test_a_delivery_receipt_reads_as_delivered_not_opened(sms_client):
    """SMS has no read receipt, and a delivery is never an open."""
    from datetime import UTC, datetime

    ev = _make_event(sms_client, sms_to="+15551110000")
    db, _ = _db_and_user()
    r = db.execute(select(Recipient).where(Recipient.event_id == ev)).scalars().one()
    r.sms_delivered_at = datetime.now(UTC)
    db.commit()
    body = sms_client.get(f"/events/{ev}").text
    assert "Delivered by text" in body
    assert "Read on" not in body
    assert r.first_open_at is None


def test_no_receipt_line_before_anything_is_delivered(sms_client):
    ev = _make_event(sms_client, sms_to="+15551110000")
    assert "Delivered by text" not in sms_client.get(f"/events/{ev}").text


# --- the send button tells the truth ------------------------------------------

def test_the_button_names_text_for_an_sms_only_card(sms_client, monkeypatch):
    monkeypatch.setenv("KITH_SEND_MODE", "live")
    get_settings.cache_clear()
    ev = _make_event(sms_client, sms_to="+15551110000")
    body = html.unescape(sms_client.get(f"/events/{ev}").text)
    assert "this site's text number" in body
    assert "your Gmail" not in body


def test_the_button_names_both_for_an_email_and_text_card(sms_client, monkeypatch):
    monkeypatch.setenv("KITH_SEND_MODE", "live")
    get_settings.cache_clear()
    ev = _make_event(sms_client, emails="ali@example.com", sms_to="+15551110000")
    body = html.unescape(sms_client.get(f"/events/{ev}").text)
    assert "your Gmail and text" in body


def test_the_button_names_all_three(both_client, monkeypatch):
    monkeypatch.setenv("KITH_SEND_MODE", "live")
    get_settings.cache_clear()
    ev = _make_event(
        both_client, emails="ali@example.com", wa_to="+15551110000", sms_to="+15552220000",
    )
    body = html.unescape(both_client.get(f"/events/{ev}").text)
    assert "your Gmail, WhatsApp and text" in body


@pytest.mark.parametrize(
    ("channels", "via", "dest"),
    [
        (["email"], "by email", "your Gmail"),
        (["whatsapp"], "over WhatsApp", "your WhatsApp"),
        (["sms"], "by text", "this site's text number"),
        (["email", "whatsapp"], "by email and WhatsApp", "your Gmail and WhatsApp"),
        (["email", "sms"], "by email and text", "your Gmail and text"),
        (["whatsapp", "sms"], "by WhatsApp and text", "your WhatsApp and text"),
        (["email", "whatsapp", "sms"],
         "by email, WhatsApp and text", "your Gmail, WhatsApp and text"),
    ],
)
def test_the_send_wording_names_exactly_the_channels_in_play(channels, via, dest):
    """The whole matrix, because the template only surfaces some of it.

    The email+WhatsApp row is the pre-existing copy and must not drift; the
    others are the new combinations.
    """
    from kith.web.routes_events import _send_ui

    rows = [Recipient(channel=ch, phone=None if ch == "email" else "+1555") for ch in channels]
    _, hint, confirm = _send_ui("live", rows, "invitation")
    assert dest in hint and dest in confirm
    _, self_hint, _ = _send_ui("self-only", rows, "invitation")
    assert f"({via})" in self_hint


def test_an_empty_queue_still_words_the_button_without_crashing():
    """Nothing queued: the button says "Nothing to send", but the helper still
    has to return something rather than index an empty list."""
    from kith.web.routes_events import _send_ui

    label, hint, confirm = _send_ui("live", [], "invitation")
    assert label and hint and confirm


# --- the preview --------------------------------------------------------------

def test_the_preview_shows_the_text_and_its_segment_count(sms_client):
    ev = _make_event(sms_client, sms_to="Mara <+15551110000>")
    body = html.unescape(sms_client.get(f"/events/{ev}").text)
    assert "What the text will say" in body
    assert "Hi Mara - it's Ilia." in body
    assert "1 segment" in body
    assert "2 segments" not in body


def test_a_long_note_shows_a_higher_segment_count(sms_client):
    """The number is the point: a host can shorten the title while they can
    still see it, and cannot once the texts have gone."""
    r = sms_client.post(
        "/events",
        data={"title": "Joe's 3rd Birthday " + "and everyone is invited " * 8,
              "event_date": "2099-06-14", "event_time": "15:00",
              "recipients": "", "wa_recipients": "", "sms_recipients": "+15551110000",
              "block_rsvp": "on", "block_date": "on", "block_time": "on"},
        follow_redirects=False,
    )
    ev = r.headers["location"].split("/events/")[1].split("?")[0]
    body = html.unescape(sms_client.get(f"/events/{ev}").text)
    m = re.search(r"(\d+) segments? —", body)
    assert m and int(m.group(1)) > 1


def test_there_is_no_sms_preview_without_sms_recipients(sms_client):
    ev = _make_event(sms_client, emails="ali@example.com")
    assert "What the text will say" not in sms_client.get(f"/events/{ev}").text


def test_the_whatsapp_preview_does_not_pick_up_an_sms_recipient(both_client):
    """Both channels carry a number, so a preview keyed on "has a phone" would
    show a WhatsApp message for a card that has no WhatsApp guests."""
    ev = _make_event(both_client, sms_to="+15551110000")
    body = both_client.get(f"/events/{ev}").text
    assert "What the text will say" in body
    assert "What the WhatsApp message will say" not in body


# --- the contacts page --------------------------------------------------------

def test_the_phone_field_is_no_longer_labelled_whatsapp(sms_client):
    """Scoped to the page's own markup: the stylesheet in <head> mentions
    WhatsApp for the linking page's rules, which is not what this is about."""
    body = sms_client.get("/contacts").text
    page = body.split("</style>", 1)[-1]
    assert 'name="phone"' in page
    assert "Mobile number" in page
    # Scoped to the field's own row, not the whole page: a nav or footer that
    # one day mentions WhatsApp is not this test's business.
    field = page[page.index('name="phone"') - 400: page.index('name="phone"') + 400]
    assert "WhatsApp" not in field
    assert "mobile number" in page          # the softened hint


def test_the_phone_field_is_absent_when_neither_phone_channel_is_on(off_client):
    body = off_client.get("/contacts").text
    assert 'name="phone"' not in body


# --- nothing regressed for WhatsApp -------------------------------------------

def test_the_whatsapp_box_and_badge_still_work(both_client):
    ev = _make_event(both_client, wa_to="+15551110000")
    new = both_client.get("/events/new").text
    assert 'name="wa_recipients"' in new and "Anyone on WhatsApp?" in new
    body = both_client.get(f"/events/{ev}").text
    assert "· WhatsApp" in body and "· SMS" not in body
    assert "What the WhatsApp message will say" in body


# --- switching the channel off must not delete anyone --------------------------

def test_switching_sms_off_keeps_sms_guests_through_an_edit(sms_client, monkeypatch):
    """The compose form hides the SMS box when the channel is off, so an edit
    posts nothing for it. That must read as "leave them alone", not "remove
    them" — the plan's own advice is to provision a number only for the month
    of the event, which makes switching off an ordinary thing to do."""
    from kith.core.channels import CHANNEL_SMS

    r = sms_client.post(
        "/events",
        data={"title": "Party", "event_date": "2099-06-14",
              "recipients": "mara@example.com", "sms_recipients": "+15551110000",
              "block_date": "on"},
        follow_redirects=False,
    )
    ev = r.headers["location"].split("/events/")[1].split("?")[0]
    db, _ = _db_and_user()
    before = {(x.channel, x.id) for x in
              db.execute(select(Recipient).where(Recipient.event_id == ev)).scalars()}
    assert CHANNEL_SMS in {c for c, _ in before}

    off = _fresh_client(monkeypatch, KITH_SMS_ENABLED="false", KITH_SMS_PROVIDER="none")
    try:
        page = off.get(f"/events/{ev}/edit").text
        assert 'name="sms_recipients"' not in page
        off.post(
            f"/events/{ev}",
            data={"title": "Party, renamed", "event_date": "2099-06-14",
                  "recipients": "mara@example.com", "block_date": "on"},
            follow_redirects=False,
        )
    finally:
        off.__exit__(None, None, None)
    db, _ = _db_and_user()
    after = {(x.channel, x.id) for x in
             db.execute(select(Recipient).where(Recipient.event_id == ev)).scalars()}
    assert after == before, "an edit with the box hidden must not touch its rows"


# --- why the texts are stuck, on every page load ------------------------------

def _sms_event(client):
    r = client.post(
        "/events",
        data={"title": "Party", "event_date": "2099-06-14", "recipients": "",
              "sms_recipients": "+15551110000", "block_date": "on"},
        follow_redirects=False,
    )
    return r.headers["location"].split("/events/")[1].split("?")[0]


def test_a_stopped_sms_batch_explains_itself_on_later_loads(sms_client):
    """The batch runs after the response, so the redirect can't carry the reason;
    the dashboard reads it from the site-wide note the batch leaves instead."""
    from kith.services import send as sender

    ev = _sms_event(sms_client)
    sender._remember_sms_block("misconfigured")
    try:
        body = html.unescape(sms_client.get(f"/events/{ev}").text)
        assert "rejected this site's setup" in body
    finally:
        sender._remember_sms_block(None)
    body = html.unescape(sms_client.get(f"/events/{ev}").text)
    assert "rejected this site's setup" not in body


def test_self_only_without_a_test_number_says_texts_are_held(sms_client, monkeypatch):
    """Both the stuck note on the page and the button's hint say so — and stop
    saying so the moment a test number is configured. (The hint is asserted on
    the helper: the detail template shows the label and confirmation but has
    never rendered the hint, for any channel.)"""
    from kith.web.routes_events import _send_ui

    ev = _sms_event(sms_client)
    monkeypatch.setenv("KITH_SEND_MODE", "self-only")
    get_settings.cache_clear()
    body = html.unescape(sms_client.get(f"/events/{ev}").text)
    assert "has no test number for texts" in body

    db, _ = _db_and_user()
    rows = db.execute(select(Recipient).where(Recipient.event_id == ev)).scalars().all()
    _, hint, _ = _send_ui("self-only", rows, "invitation", sms_test_number=False)
    assert "Sends only to you (by text)" in hint and "Texts are held" in hint
    _, hint, _ = _send_ui("self-only", rows, "invitation", sms_test_number=True)
    assert "Texts are held" not in hint

    monkeypatch.setenv("KITH_SMS_SELF_NUMBER", "+15550009999")
    get_settings.cache_clear()
    body = html.unescape(sms_client.get(f"/events/{ev}").text)
    assert "has no test number" not in body


def test_the_segment_hint_explains_a_non_gsm_title(sms_client):
    r = sms_client.post(
        "/events",
        data={"title": "Maya turns five! \U0001F389", "event_date": "2099-06-14",
              "recipients": "", "sms_recipients": "+15551110000", "block_date": "on"},
        follow_redirects=False,
    )
    ev = r.headers["location"].split("/events/")[1].split("?")[0]
    body = html.unescape(sms_client.get(f"/events/{ev}").text)
    assert "70 characters here" in body and "emoji or a curly quote" in body
