import base64
import email

from kith.core.mailbuild import build_email, invite_html, invite_text, subject_for, to_raw


def test_subject():
    assert subject_for("Party", True) == "You're invited: Party"
    assert subject_for("Holiday card", False) == "Holiday card"
    assert subject_for("", True).endswith("A card for you")


def test_html_has_cid_link_and_escapes():
    html = invite_html(
        title="A & B", message="Line1\nLine2", host_name="Mara", recipient_name="Sam",
        view_url="https://x.ts.net/i/tok", has_image=True, rsvp=True,
    )
    assert "cid:cardimage" in html
    assert "https://x.ts.net/i/tok" in html
    assert "A &amp; B" in html          # title escaped
    assert "Line1<br>Line2" in html     # newline -> <br>
    assert "Hi Sam," in html


def test_text_has_link_and_greeting():
    t = invite_text(
        title="Party", message="", host_name="Mara", recipient_name=None,
        view_url="https://x/i/tok", rsvp=True,
    )
    assert "https://x/i/tok" in t
    assert "Hi there," in t


def test_build_email_is_multipart_with_inline_image():
    msg = build_email(
        subject="S", from_name="Mara", from_email="mara@example.com",
        to_email="a@example.com", to_name="Sam", html="<p>cid:cardimage</p>", text="hi",
        image_bytes=b"fakepngbytes", image_subtype="png",
    )
    assert msg["Subject"] == "S"
    assert "mara@example.com" in msg["From"]
    assert "a@example.com" in msg["To"]
    types = {p.get_content_type() for p in msg.walk()}
    assert {"text/plain", "text/html", "image/png"} <= types


def test_to_raw_roundtrips():
    msg = build_email(
        subject="S", from_name="M", from_email="m@x.com", to_email="a@x.com",
        html="<p>x</p>", text="x",
    )
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(to_raw(msg)))
    assert parsed["Subject"] == "S"
