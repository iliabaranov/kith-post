"""Database models."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from kith.db.session import Base
from kith.db.types import EncryptedString


def _uuid() -> str:
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    # Google's stable subject id — opaque, used to look up the account on login.
    google_sub: Mapped[str] = mapped_column(String, unique=True, index=True)
    # PII + token are encrypted at rest (see EncryptedString).
    email: Mapped[str] = mapped_column(EncryptedString)
    refresh_token: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    display_name: Mapped[str] = mapped_column(String, default="")
    # set when a Gmail send fails because the refresh token expired/was revoked;
    # drives the "reconnect Google" prompt, cleared on the next successful auth/send.
    reconnect_needed: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    # --- WhatsApp channel link (the credentials live in WAHA's volume, never here) ---
    # WAHA session name for this user; set once the host opts in, cleared on unlink.
    wa_session: Mapped[str | None] = mapped_column(String, nullable=True)
    # Last status we saw from WAHA (WORKING, SCAN_QR_CODE, FAILED, ...). A cache for
    # the UI, never trusted at send time — the send path re-reads it from WAHA.
    wa_status: Mapped[str | None] = mapped_column(String, nullable=True)
    wa_status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    wa_linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The linked WhatsApp number, shown back to the host so they can tell which
    # account is connected. PII, so encrypted like every other number we hold.
    wa_number: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    # When WhatsApp's reachout timelock (error 463) lifts; set when we hit one so
    # the UI can explain the pause instead of just failing. NULL = not restricted.
    wa_timelock_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Last seen new-chat quota state ({status, total, used, cycle_end}), for the UI.
    wa_capping: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The host has read the "WAHA is an unofficial client and your account could be
    # banned" warning. No session is created before this is set.
    wa_risk_ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Contact(Base):
    """A person in a user's reusable address book. Email/phone/name are encrypted
    at rest; the *_hash columns are blind indexes (keyed HMAC of the normalized
    value) so we can dedupe and look up without storing or querying plaintext.

    ``email_hash`` is really the *identity* hash: HMAC of the email when there is
    one, else of "tel:<e164>". A WhatsApp-only contact has no email, but the
    column is NOT NULL and carries the per-user UNIQUE constraint, so hashing the
    phone into it is what keeps phone-only people distinct from each other
    (see ``services.contacts.identity_hash``). ``email`` itself is "" for them.
    """

    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("user_id", "email_hash", name="uq_contact_user_email"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(EncryptedString)
    name: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    email_hash: Mapped[str] = mapped_column(String, index=True)
    # WhatsApp number in E.164, encrypted, with its own blind index for lookups.
    phone: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    phone_hash: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # free-form group tags, e.g. ["family", "local"]; None on legacy rows == no tags
    groups: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Asset(Base):
    """An uploaded card image, sanitized + stored as a full-res and inline copy."""

    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    sha256: Mapped[str] = mapped_column(String)
    mime: Mapped[str] = mapped_column(String)
    full_path: Mapped[str] = mapped_column(String)
    inline_path: Mapped[str] = mapped_column(String)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Event(Base):
    """One card / occasion. `blocks` decides which sections the recipient sees."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    event_time: Mapped[str | None] = mapped_column(String, nullable=True)  # "HH:MM" start
    event_end_time: Mapped[str | None] = mapped_column(String, nullable=True)  # "HH:MM" end
    timezone: Mapped[str | None] = mapped_column(String, nullable=True)  # IANA tz for calendar
    location: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)  # PII
    signoff: Mapped[str | None] = mapped_column(String, nullable=True)  # signature line
    # frame/background treatment of the invitation card; NULL = "washi" (the default)
    card_style: Mapped[str | None] = mapped_column(String, nullable=True)
    # if set, the sweep worker auto-sends the card at this UTC time; cleared once sent
    scheduled_send_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # optional Cc list (encrypted JSON [{name,email}]) added to the email; only for
    # cards without RSVP. CC'd people get no personal invite/link/tracking.
    cc: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    # blocks = which sections the recipient sees: date/time/location/message/rsvp/headcount
    blocks: Mapped[dict] = mapped_column(JSON, default=dict)
    headcount_max: Mapped[int | None] = mapped_column(Integer, nullable=True)  # cap per invite
    asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="draft")
    # Set when a WhatsApp send for this card is handed off, cleared when a batch
    # completes a full pass. While it is set with recipients still queued, a batch
    # is owed — which is how an interrupted one is resumed after a restart. This
    # is the whole durable-queue mechanism: the work itself is already durable,
    # since a pending recipient is a row with status 'queued'.
    wa_batch_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reminder_cfg: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # per-event (G5)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Recipient(Base):
    """One (event, person). status = latest answer; PII encrypted at rest.

    A recipient is reached over exactly one channel. ``channel`` is NULL on every
    row written before the WhatsApp channel existed, which is why NULL means
    email — see ``CHANNEL_EMAIL``. For a WhatsApp or SMS recipient ``email`` is
    "" and ``phone`` holds the E.164 number, which is why the channel column
    rather than the number is what says which of the two it is;
    ``email`` stays NOT NULL because the
    additive schema sync (and SQLite) can't loosen an existing column, and a
    table rebuild on a live database isn't worth it for a sentinel.
    """

    __tablename__ = "recipients"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(EncryptedString)
    # "email" (or NULL, for rows that predate the channel) | "whatsapp" | "sms"
    channel: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    name: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    token: Mapped[str] = mapped_column(String, unique=True, index=True)
    status: Mapped[str] = mapped_column(String, default="queued")
    party_size: Mapped[int | None] = mapped_column(Integer, nullable=True)  # total = adults + kids
    adults: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kids: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)  # optional reply note
    # allergy / dietary note (only when the event asks)
    allergies: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_open_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rsvp_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # WAHA's message id for a WhatsApp send, so a reminder can thread under it.
    wa_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # WhatsApp's own receipts for that message, pushed back by WAHA. These are
    # the channel's delivery facts — the same ticks the host sees in their own
    # WhatsApp — and are deliberately kept separate from `first_open_at`, which
    # means a person opened the invitation page.
    wa_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    wa_read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Highest ack seen: -1 error, 0 pending, 1 server, 2 device, 3 read, 4 played.
    wa_ack: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The provider's own id for an SMS send, so a delivery receipt arriving later
    # can be matched back to the recipient it belongs to.
    sms_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # The carrier said it arrived. There is no read receipt for SMS, so this is
    # the only delivery fact the channel offers — and, like the wa_* pair above,
    # it is kept well away from `first_open_at`: a delivery is not an open.
    sms_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The carrier refused it. Kept because it is the host's only signal that a
    # number is bad — without it a text that never arrived reads "sent" for ever.
    sms_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    msg_id_hdr: Mapped[str | None] = mapped_column(String, nullable=True)  # Gmail resource id
    thread_id: Mapped[str | None] = mapped_column(String, nullable=True)   # Gmail threadId
    # RFC822 Message-ID we stamp on the first send; reminders/re-sends reference it
    # (In-Reply-To/References) so Gmail threads them as one conversation.
    rfc822_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Reminder(Base):
    """One scheduled nudge for a non-responding recipient (G5, §8). Persisted so the
    sweep is downtime-safe: any pending row whose time has passed fires on the next
    tick. FKs cascade, so deleting a recipient/event cleans up its reminders."""

    __tablename__ = "reminders"
    __table_args__ = (Index("ix_reminders_due", "status", "scheduled_for"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    recipient_id: Mapped[str] = mapped_column(
        ForeignKey("recipients.id", ondelete="CASCADE"), index=True
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # UTC
    offset_label: Mapped[str] = mapped_column(String)  # "halfway" | "7d" | "3d" | "manual"
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|sent|skipped|canceled
    skip_reason: Mapped[str | None] = mapped_column(String, nullable=True)  # past|engaged|capped
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SmsOptOutEvent(Base):
    """One STOP or START reply, as it arrived. Append-only; deleted by nobody.

    The suppression list is "every number whose latest event is a stop" — see
    ``services.send.opted_out_hashes``. It is a table of its own, keyed on the
    number's blind index, because the two obvious homes for a flag are both the
    host's to delete: a recipient row goes with its event, a contact with the
    address book, and an opt-out has to outlive both. No FK, no cascade and no
    user_id — the site texts from one number, so a STOP binds every host on it.
    Holding a hash rather than the number is what lets it survive an account
    delete without keeping anything readable.
    """

    __tablename__ = "sms_opt_out_events"

    # Integer and ascending, unlike the UUID ids elsewhere: "latest per number"
    # is the one query this table exists for, and max(id) answers it.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone_hash: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)          # "stop" | "start"
    source: Mapped[str] = mapped_column(String)        # "twilio" | "gateway"
    # The provider's id for the inbound message. Unique, so a replayed POST —
    # Twilio's signature carries no timestamp — records nothing a second time.
    message_sid: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SmsLink(Base):
    """A host's own SMS provider — their phone as the sender, or their Twilio.

    The per-host counterpart of the WhatsApp link, with one structural
    difference: WhatsApp's credentials live in WAHA's volume and we keep only a
    session name, whereas here there is no external vault, so the credentials
    themselves are ours to hold. Every secret and every phone number is an
    ``EncryptedString``; what stays readable is what a lookup needs — the
    Twilio account SID, which a status callback carries and is how the webhook
    finds the host it belongs to, and the random URL token that does the same
    job for the gateway. One row per host: a host has one way of texting at a
    time, and switching provider replaces it.
    """

    __tablename__ = "sms_links"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String)            # "gateway" | "twilio"

    # --- the phone route (capcom6 SMS Gateway for Android) ---
    gateway_url: Mapped[str | None] = mapped_column(String, nullable=True)
    gateway_user: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    gateway_pass: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    gateway_path: Mapped[str | None] = mapped_column(String, nullable=True)
    gateway_device_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # The app's message-encryption passphrase (Settings -> Encryption on the
    # phone). Set: text and numbers leave here as ciphertext only the phone can
    # read. Unset: plaintext, which over a tailnet or LAN is still inside an
    # encrypted hop. See kith.services.sms_crypto.
    gateway_passphrase: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)

    # --- the Twilio route ---
    # Plain and indexed on purpose: Twilio posts AccountSid with every callback,
    # and it is how an inbound STOP is matched to the host whose token verifies
    # it. Not a secret — it is in every Twilio URL.
    twilio_account_sid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    twilio_auth_token: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    twilio_from: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    twilio_messaging_service_sid: Mapped[str | None] = mapped_column(String, nullable=True)

    # The number guests see the texts come from — the SIM in the gateway phone,
    # or the Twilio number. Optional for the gateway; it is what lets a STOP
    # that arrives as a national number ("6505551212") be resolved to E.164.
    sender_number: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    # Where a self-only test send goes. This host's, not the site's.
    self_number: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)

    # Generated here and typed into the gateway app as its webhook signing key.
    # Twilio does not use it: its callbacks are verified with the auth token.
    webhook_secret: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    # Routes the gateway's POSTs to this host: /sms/webhook/gateway/<token>.
    # Random and unique, but not a credential — the signature is the credential.
    webhook_token: Mapped[str] = mapped_column(String, unique=True, index=True)
    webhooks_registered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # What the page shows as status. A test send is the only way to learn the
    # credentials work before a real card depends on them.
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
