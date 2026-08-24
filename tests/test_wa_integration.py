"""Does the WhatsApp channel actually reach every corner of the app?

Adding a column and a send path is the easy half. This file is the audit: every
place that lists people, imports them, names a channel, or tells the host what is
about to happen. Each test here corresponds to something that was wrong after the
feature "worked".
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kith.config import get_settings
from kith.core import eventkind, wamessage
from kith.core.recipients import parse_mixed
from kith.db.models import Recipient, User
from kith.services import contacts as book
from kith.services import waha


def _db_and_user():
    from kith.db.session import make_engine, make_session_factory

    db = make_session_factory(make_engine(get_settings().db_path))()
    return db, db.execute(select(User)).scalars().first()


@pytest.fixture
def wa(monkeypatch):
    """Channel on, host linked. No WAHA calls are made by these tests."""
    monkeypatch.setenv("KITH_WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("KITH_WAHA_API_KEY", "test-key")
    # live, like the deploy box: dry-run copy deliberately names no channel,
    # because nothing is going anywhere.
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


@pytest.fixture
def wa_off():
    """A fresh app with the channel off, independent of import order."""
    get_settings.cache_clear()
    try:
        from kith.web.app import create_app

        with TestClient(create_app()) as c:
            c.post("/auth/dev-login")
            yield c
    finally:
        get_settings.cache_clear()


# --- pasting a mixed list -----------------------------------------------------

def test_pasting_a_mixed_list_sorts_people_by_what_they_gave(wa):
    wa.post("/contacts/import", data={"people":
        "Ali <ali@example.com>\n+1 555 111 0000\nMara <+14085559090>\nnonsense"})
    db, user = _db_and_user()
    assert book.find_by_email(db, user.id, "ali@example.com") is not None
    assert book.find_by_phone(db, user.id, "+15551110000") is not None
    assert book.find_by_phone(db, user.id, "+14085559090").name == "Mara"


def test_parse_mixed_reports_what_it_could_not_read():
    valid, invalid = parse_mixed("a@x.com\n+15551110000\n555 111 0000\nzzz")
    assert [p.channel for p in valid] == ["email", "whatsapp"]
    assert invalid == ["555 111 0000", "zzz"]


def test_parse_mixed_dedupes_the_same_person_twice():
    valid, _ = parse_mixed("+1 555 111 0000\n+15551110000\na@x.com\nA@X.com")
    assert len(valid) == 2


# --- CSV ----------------------------------------------------------------------

def _csv(client, text):
    return client.post(
        "/contacts/import-csv",
        files={"file": ("book.csv", text, "text/csv")},
        follow_redirects=False,
    )


def test_csv_with_a_header_takes_a_phone_column(wa):
    _csv(wa, "name,email,phone,groups\n"
             "Ali,ali@example.com,+15551110000,family\n"
             "Mara,,+14085559090,family\n")
    db, user = _db_and_user()
    ali = book.find_by_email(db, user.id, "ali@example.com")
    assert ali.phone == "+15551110000" and ali.groups == ["family"]
    mara = book.find_by_phone(db, user.id, "+14085559090")
    assert mara.name == "Mara" and mara.email == ""


def test_csv_header_columns_can_be_in_any_order(wa):
    _csv(wa, "groups,whatsapp,name,e-mail\nwork,+15551110000,Ali,ali@example.com\n")
    db, user = _db_and_user()
    ali = book.find_by_email(db, user.id, "ali@example.com")
    assert ali.phone == "+15551110000" and ali.name == "Ali" and ali.groups == ["work"]


def test_a_headerless_csv_keeps_its_old_meaning(wa):
    """The third column has always been groups. Reading it as a phone would
    quietly mangle every file anyone already has."""
    _csv(wa, "Ali,ali@example.com,family\nSam,sam@example.com,\"work, local\"\n")
    db, user = _db_and_user()
    ali = book.find_by_email(db, user.id, "ali@example.com")
    assert ali.groups == ["family"] and ali.phone is None
    sam = book.find_by_email(db, user.id, "sam@example.com")
    assert sam.groups == ["work", "local"]


def test_a_csv_row_with_no_usable_address_is_reported(wa):
    r = _csv(wa, "name,email,phone\nNobody,,\n")
    assert r.status_code == 303
    db, user = _db_and_user()
    assert book.list_contacts(db, user.id) == []


def test_a_csv_number_without_a_country_code_is_refused(wa):
    _csv(wa, "name,email,phone\nAli,,555 111 0000\n")
    db, user = _db_and_user()
    assert book.list_contacts(db, user.id) == []


def test_the_template_and_export_round_trip_numbers(wa):
    template = wa.get("/contacts/template.csv").text
    assert template.splitlines()[0] == "name,email,phone,groups"
    # A phone-only row in the template must actually import.
    _csv(wa, template)
    db, user = _db_and_user()
    assert book.find_by_phone(db, user.id, "+14085559090") is not None

    exported = wa.get("/contacts/export").text
    assert exported.splitlines()[0] == "name,email,phone,groups"
    assert "+14085559090" in exported
    # ...and re-importing an export adds nobody new (it's the same people).
    before = len(book.list_contacts(db, user.id))
    _csv(wa, exported)
    db2, user2 = _db_and_user()
    assert len(book.list_contacts(db2, user2.id)) == before


# --- the compose picker -------------------------------------------------------

def test_the_picker_offers_numbers_and_a_channel_choice(wa):
    db, user = _db_and_user()
    book.add_contact(db, user.id, "both@example.com", "Both", phone="+15551110000")
    book.add_contact(db, user.id, "", "PhoneOnly", phone="+14085559090")
    book.add_contact(db, user.id, "mail@example.com", "MailOnly")
    body = wa.get("/events/new").text
    assert 'data-phone="+15551110000"' in body      # a contact with both
    assert 'data-phone="+14085559090"' in body      # ...and one with only a number
    assert 'class="book-ch"' in body                # the choice, for the one with both
    assert "book-ch-fixed" in body                  # "WhatsApp", for the phone-only one
    assert "+14085559090" in body                   # shown, not hidden behind a blank


def test_the_picker_hides_numbers_when_the_host_cannot_send_them(wa_off):
    db, user = _db_and_user()
    book.add_contact(db, user.id, "both@example.com", "Both", phone="+15551110000")
    body = wa_off.get("/events/new").text
    assert "data-phone=\"+15551110000\"" not in body
    assert 'class="book-ch"' not in body


def test_the_picker_targets_the_whatsapp_box(wa):
    """The routing logic lives in the page's JS; assert it addresses both boxes."""
    body = wa.get("/events/new").text
    assert 'id="wa_recipients"' in body
    assert 'getElementById("wa_recipients")' in body


# --- the dashboard ------------------------------------------------------------

def _event(client, *, emails="", phones="", title="Party", rsvp=True, date=""):
    data = {"title": title, "recipients": emails, "wa_recipients": phones}
    if rsvp:
        data["block_rsvp"] = "on"
    if date:
        data["event_date"], data["block_date"] = date, "on"
    r = client.post("/events", data=data, follow_redirects=False)
    return r.headers["location"].split("/events/")[1].split("?")[0]


def test_an_unnamed_whatsapp_recipient_is_not_a_blank_row(wa):
    """Their email column is "" (the NOT NULL sentinel), so the dashboard used to
    show a nameless, addressless row."""
    ev = _event(wa, phones="+15551110000")
    body = wa.get(f"/events/{ev}").text
    assert "+15551110000" in body
    assert "WhatsApp" in body            # and which channel they're on


def test_a_named_whatsapp_recipient_shows_name_and_number(wa):
    ev = _event(wa, phones="Mara <+15551110000>")
    body = wa.get(f"/events/{ev}").text
    assert "Mara" in body and "+15551110000" in body


def test_email_recipients_are_unmarked(wa):
    ev = _event(wa, emails="ali@example.com")
    body = wa.get(f"/events/{ev}").text
    assert "ali@example.com" in body
    assert "· WhatsApp" not in body


# --- what the host is told before sending -------------------------------------

def test_the_confirmation_names_whatsapp_for_a_whatsapp_card(wa):
    ev = _event(wa, phones="+15551110000")
    body = wa.get(f"/events/{ev}").text
    assert "your WhatsApp" in body
    assert "your Gmail" not in body


def test_the_confirmation_names_both_for_a_mixed_card(wa):
    ev = _event(wa, emails="ali@example.com", phones="+15551110000")
    body = wa.get(f"/events/{ev}").text
    assert "your Gmail and WhatsApp" in body


def test_the_confirmation_still_says_gmail_for_an_email_card(wa):
    ev = _event(wa, emails="ali@example.com")
    body = wa.get(f"/events/{ev}").text
    assert "from your Gmail now?" in body
    assert "your Gmail and WhatsApp" not in body
    assert "from your WhatsApp" not in body


def test_the_confirmation_counts_properly_and_names_the_thing(wa):
    ev = _event(wa, emails="a@example.com,b@example.com", rsvp=True, date="2099-01-01")
    body = wa.get(f"/events/{ev}").text
    assert "2 invitations" in body      # not "2 invitation(s)"
    ev2 = _event(wa, emails="a@example.com", rsvp=False)
    assert "1 card" in wa.get(f"/events/{ev2}").text


def test_google_is_not_blamed_when_only_whatsapp_is_queued(wa):
    ev = _event(wa, phones="+15551110000")
    db, user = _db_and_user()
    user.refresh_token = None
    db.commit()
    body = wa.get(f"/events/{ev}?failed=1").text
    assert "sign in with Google" not in body


# --- card vs invitation -------------------------------------------------------

def test_a_card_is_not_announced_as_an_invitation():
    text = wamessage.invite_text(
        title="Love you", host_name="Ilia Baranov",
        view_url="https://kithpo.st/i/abc", rsvp=False, invitation=False,
    )
    assert "You're invited" not in text
    assert "I've sent you a card: Love you." in text


def test_an_invitation_still_reads_as_one():
    text = wamessage.invite_text(
        title="Joe's 3rd Birthday", host_name="Ilia",
        view_url="https://kithpo.st/i/abc", rsvp=True, invitation=True,
    )
    assert "You're invited to Joe's 3rd Birthday." in text


def test_a_dated_card_without_rsvp_is_an_invitation_that_asks_nothing():
    from datetime import date

    assert eventkind.is_invitation({"date": True}, date(2099, 1, 1)) is True
    assert eventkind.is_invitation({"rsvp": True}, None) is True
    assert eventkind.is_invitation({}, None) is False
    text = wamessage.invite_text(
        title="Our wedding", host_name="Ilia", view_url="https://x/i/a",
        when="Fri, Jan 01", rsvp=False, invitation=True,
    )
    assert "You're invited to Our wedding." in text
    assert "let me know" not in text     # no RSVP to give


def test_a_reminder_about_a_card_does_not_call_it_an_invitation():
    text = wamessage.reminder_text(
        title="", host_name="Ilia", view_url="https://x/i/a",
        rsvp=False, invitation=False,
    )
    assert "invitation" not in text
    assert "the card I sent" in text


def test_the_link_preview_matches_what_was_sent(wa):
    """The invite page's <title> is what a chat app shows in its preview, so a
    card must not be previewed as "You're invited"."""
    card = _event(wa, phones="+15551110000", title="Love you", rsvp=False)
    invite = _event(wa, phones="+15552220000", title="Party", rsvp=True)
    db, _ = _db_and_user()
    tokens = {
        r.event_id: r.token
        for r in db.execute(select(Recipient)).scalars().all()
    }
    card_page = wa.get(f"/i/{tokens[card]}").text
    invite_page = wa.get(f"/i/{tokens[invite]}").text
    assert "<title>A card for you" in card_page
    assert "You're invited" not in card_page.split("</head>")[0]
    assert "<title>You&#39;re invited" in invite_page or "<title>You're invited" in invite_page


# --- the legal pages ----------------------------------------------------------

def test_privacy_discloses_numbers_and_the_whatsapp_channel(wa):
    body = wa.get("/privacy").text
    assert "phone numbers" in body
    assert "WhatsApp" in body
    assert "unofficial" in body                    # the risk is named
    assert "never" in body and "WhatsApp login" in body  # we don't hold the login


def test_terms_names_the_whatsapp_risk(wa):
    body = wa.get("/terms").text
    assert "unofficial" in body and "restricted or banned" in body
    assert "Meta Platforms" in body                # trademark attribution


def test_the_legal_pages_say_nothing_about_whatsapp_when_it_is_off(wa_off):
    privacy, terms = wa_off.get("/privacy").text, wa_off.get("/terms").text
    for body in (privacy, terms):
        assert "unofficial" not in body
        assert "restricted or banned" not in body
    assert "WhatsApp (optional)" not in privacy
    assert "Meta Platforms" not in terms


# --- copy that quietly assumed email ------------------------------------------

def test_the_title_hint_does_not_promise_a_subject_line_on_whatsapp(wa):
    body = wa.get("/events/new").text
    assert "as the subject line" in body and "when it goes by email" in body


def test_cc_says_it_cannot_reach_whatsapp_guests(wa):
    """Cc is applied only to the email MIME, so on a WhatsApp-only card it does
    nothing at all. Better to say so than to let a host assume otherwise."""
    body = wa.get("/events/new").text
    assert "Email only" in body
    assert "never messaged there" in body or "never messaged" in body


def test_those_hints_stay_email_only_when_the_channel_is_off(wa_off):
    body = wa_off.get("/events/new").text
    assert "used as the email's subject line" in body
    assert "Email only" not in body


# --- the "Opened" signal must stay honest -------------------------------------

def _one_recipient(client):
    ev = _event(client, phones="+15551110000", rsvp=True)
    db, _ = _db_and_user()
    r = db.execute(
        select(Recipient).where(Recipient.event_id == ev)
    ).scalars().first()
    return db, r


@pytest.mark.parametrize("ua", [
    "WhatsApp/2.2437.4 A",
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "TelegramBot (like TwitterBot)",
    "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
    "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
    "Twitterbot/1.0",
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
])
def test_a_link_preview_crawler_does_not_count_as_opened(wa, ua):
    """The bug this exists for: WhatsApp fetches the invitation page to build its
    preview, which recorded an open 0.4s *before* the message finished sending."""
    db, r = _one_recipient(wa)
    assert r.first_open_at is None
    page = wa.get(f"/i/{r.token}", headers={"user-agent": ua})
    assert page.status_code == 200, "the page must still render — the preview is wanted"
    db2, _ = _db_and_user()
    assert db2.get(Recipient, r.id).first_open_at is None


def test_a_real_visit_still_counts_as_opened(wa):
    db, r = _one_recipient(wa)
    wa.get(f"/i/{r.token}", headers={"user-agent":
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36"})
    db2, _ = _db_and_user()
    assert db2.get(Recipient, r.id).first_open_at is not None


def test_a_speculative_prefetch_does_not_count_as_opened(wa):
    db, r = _one_recipient(wa)
    wa.get(f"/i/{r.token}", headers={
        "user-agent": "Mozilla/5.0 (Macintosh) Safari/605.1.15",
        "sec-purpose": "prefetch;anonymous-client-ip",
    })
    db2, _ = _db_and_user()
    assert db2.get(Recipient, r.id).first_open_at is None


def test_a_crawler_visit_does_not_stop_reminders(wa):
    """Opened is what cancels a nudge, so a false open silently cancels one."""
    from kith.core import reminders as rem

    db, r = _one_recipient(wa)
    r.status = "sent"          # a nudge is only owed to someone already sent to
    db.commit()
    wa.get(f"/i/{r.token}", headers={"user-agent": "WhatsApp/2.2437.4 A"})
    db2, _ = _db_and_user()
    fresh = db2.get(Recipient, r.id)
    assert rem.still_needs_nudge(fresh.status, fresh.first_open_at, "not-clicked")
    # ...and a genuine open does stop it, so the test isn't vacuous.
    assert not rem.still_needs_nudge("sent", "2026-01-01", "not-clicked")


def test_the_crawler_can_still_read_the_preview_copy(wa):
    """We refuse it the *signal*, not the page — the preview is good for guests."""
    db, r = _one_recipient(wa)
    body = wa.get(f"/i/{r.token}", headers={"user-agent": "WhatsApp/2.2437.4 A"}).text
    assert "<title>" in body and "name=\"description\"" in body
