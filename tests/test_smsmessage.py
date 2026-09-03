"""The SMS text and its segment count.

Two things carry weight here. The message must say who it is from — an SMS
arrives from a number nobody recognises, so an unattributed link is a phishing
text — and it must carry the invitation URL untouched, with nothing appended to
track anyone. The segment maths is the other half: it is what the compose
preview shows a host before they commit to sending, and it is what they are
billed on.
"""

from datetime import date

from kith.core import smsmessage as sms

URL = "https://kith.example/i/abc123"


def _invite(**kw):
    args = dict(
        title="Joe's 3rd Birthday", host_name="Ilia", view_url=URL,
        recipient_name="Mara", when="Sun, Jun 14 at 3:00 pm",
    )
    args.update(kw)
    return sms.invite_text(**args)


# --- what the invitation says -------------------------------------------------

def test_the_invitation_names_the_host_the_event_the_time_and_the_link():
    text = _invite()
    assert "Ilia" in text
    assert "Mara" in text
    assert "Joe's 3rd Birthday" in text
    assert "Sun, Jun 14 at 3:00 pm" in text
    assert URL in text


def test_the_link_is_carried_verbatim_with_nothing_appended():
    """No shortener, no redirect, no analytics parameter.

    A shortener would also hide where the link goes, which is the one property
    that most makes a text look like a scam.
    """
    text = _invite()
    assert text.rstrip().endswith(URL)
    assert text.count(URL) == 1
    assert "?" not in text.split(URL)[-1]


def test_there_is_no_card_image_and_no_markup():
    """SMS is text. WhatsApp's *bold* would render literally, and MMS is a
    different product at a different price."""
    text = _invite()
    assert "*" not in text
    assert "_" not in text
    assert "http" in text and ".jpg" not in text


def test_it_wastes_no_blank_lines():
    """Every blank line is two septets that would rather be part of the title."""
    assert "" not in _invite().split("\n")
    assert "\n\n" not in _invite()


def test_rsvp_asks_and_a_plain_card_does_not():
    assert "Can you make it?" in _invite(rsvp=True)
    text = _invite(rsvp=False)
    assert "Can you make it?" not in text
    assert "Have a look:" in text


def test_a_card_that_is_not_an_invitation_is_phrased_as_a_card():
    text = _invite(invitation=False, title="Love you")
    assert "You're invited" not in text
    assert "I've sent you a card: Love you." in text


def test_a_note_is_included_on_its_own_line():
    assert "Bring wellies" in _invite(note="  Bring wellies  ").split("\n")


def test_a_missing_name_or_host_still_greets_and_still_sends():
    assert URL in _invite(recipient_name=None)
    assert URL in _invite(host_name="")
    assert URL in _invite(recipient_name=None, host_name="", when=None, title="")


def test_a_dateless_card_simply_does_not_mention_a_date():
    text = _invite(when=None)
    assert "Jun" not in text
    assert URL in text


# --- the reminder ------------------------------------------------------------

def test_the_reminder_is_a_nudge_that_re_identifies_the_sender():
    """It arrives from the same bare number as the first one did."""
    text = sms.reminder_text(
        title="Joe's 3rd Birthday", host_name="Ilia", view_url=URL,
        recipient_name="Mara", when="Sun, Jun 14 at 3:00 pm",
    )
    assert text.startswith("Hi Mara - Ilia again.")
    assert sms.is_gsm7(text) is True
    assert "nudge" in text
    assert URL in text
    assert "You're invited" not in text     # never a second pitch


def test_the_reminder_falls_back_to_a_generic_subject():
    text = sms.reminder_text(title="", host_name="Ilia", view_url=URL)
    assert "my invitation" in text
    text = sms.reminder_text(title="", host_name="Ilia", view_url=URL, invitation=False)
    assert "the card I sent" in text


# --- when_line is the shared one ---------------------------------------------

def test_when_line_reads_the_same_as_everywhere_else():
    assert sms.when_line(date(2099, 6, 14), "15:00") == "Sun, Jun 14 at 3:00 pm"
    assert sms.when_line(date(2099, 6, 14), None) == "Sun, Jun 14"
    assert sms.when_line(None, "15:00") is None


# --- segments ----------------------------------------------------------------

def test_a_short_ascii_message_is_one_segment():
    assert sms.segments("Hi Mara - it's Ilia.") == 1
    assert sms.segments("") == 1


def test_the_greeting_avoids_the_em_dash_that_would_halve_capacity():
    """core.wamessage joins with an em dash, which is not in GSM-7.

    Reusing it verbatim would put every single text into UCS-2 and cut what fits
    from 160 characters to 70. Same words, different hyphen.
    """
    from kith.core.wamessage import _greeting as wa_greeting

    assert sms.is_gsm7(wa_greeting("Mara", "Ilia")) is False
    assert sms.is_gsm7(_invite()) is True
    assert "Hi Mara - it's Ilia." in _invite()


def test_a_realistic_invitation_fits_in_one_segment():
    """The reason the wording is terse. If this starts failing, the copy grew."""
    text = _invite()
    assert sms.is_gsm7(text), "the invitation must not tip the message into UCS-2"
    assert sms.segments(text) == 1


def test_gsm7_boundaries_are_160_then_153():
    assert sms.segments("a" * 160) == 1
    assert sms.segments("a" * 161) == 2
    assert sms.segments("a" * 200) == 2
    assert sms.segments("a" * 306) == 2      # 2 * 153
    assert sms.segments("a" * 307) == 3


def test_an_extension_character_costs_two_septets():
    """{ } [ ] ~ ^ \\ | and the euro sign are an escape plus the character."""
    assert sms.segments("a" * 158) == 1             # 158 septets
    assert sms.segments("a" * 158 + "{}") == 2      # 158 + 2*2 = 162, over the line
    assert sms.segments("{" * 80) == 1              # 160 septets exactly
    assert sms.segments("{" * 81) == 2


def test_one_emoji_drops_the_whole_message_to_ucs2():
    """There is no per-character escape: a single 🎉 halves what fits."""
    assert sms.is_gsm7("Party time") is True
    assert sms.is_gsm7("Party time 🎉") is False
    assert sms.segments("a" * 100) == 1              # comfortably GSM-7
    assert sms.segments("a" * 100 + "🎉") == 2       # same words, now UCS-2


def test_ucs2_boundaries_are_70_then_67():
    assert sms.segments("中" * 70) == 1
    assert sms.segments("中" * 71) == 2
    assert sms.segments("中" * 134) == 2         # 2 * 67
    assert sms.segments("中" * 135) == 3


def test_a_non_bmp_emoji_counts_as_two_units_not_one():
    """A surrogate pair costs the carrier two, whatever len() says."""
    assert len("🎉") == 1
    assert sms.segments("🎉" * 35) == 1              # 70 UTF-16 units
    assert sms.segments("🎉" * 36) == 2


def test_accented_latin_stays_in_gsm7():
    """à ä é ñ ö ü and friends are in the 7-bit alphabet; a curly quote is not."""
    assert sms.is_gsm7("Café à la Grüße, señor") is True
    assert sms.is_gsm7("it’s") is False         # curly apostrophe
    assert sms.is_gsm7("it's") is True
    assert sms.is_gsm7("Noël") is False        # ë is not in the alphabet; é is
    assert sms.is_gsm7("Noé") is True
