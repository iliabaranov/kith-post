"""Database models. (Event / Recipient / Tracking land in G2+.)"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
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
