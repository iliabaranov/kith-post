"""Database models."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Contact(Base):
    """A person in a user's reusable address book. Email/name are encrypted at
    rest; email_hash is a blind index (keyed HMAC of the normalized email) so we
    can dedupe and look up without storing or querying plaintext."""

    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("user_id", "email_hash", name="uq_contact_user_email"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(EncryptedString)
    name: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    email_hash: Mapped[str] = mapped_column(String, index=True)
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
    # blocks = which sections the recipient sees: date/time/location/message/rsvp/headcount
    blocks: Mapped[dict] = mapped_column(JSON, default=dict)
    headcount_max: Mapped[int | None] = mapped_column(Integer, nullable=True)  # cap per invite
    asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="draft")
    reminder_cfg: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # per-event (G5)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Recipient(Base):
    """One (event, person). status = latest answer; PII encrypted at rest."""

    __tablename__ = "recipients"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(EncryptedString)
    name: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    token: Mapped[str] = mapped_column(String, unique=True, index=True)
    status: Mapped[str] = mapped_column(String, default="queued")
    party_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_open_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rsvp_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    msg_id_hdr: Mapped[str | None] = mapped_column(String, nullable=True)  # reply-threading (G5)
    thread_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
