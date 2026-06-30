"""A SQLAlchemy column type that transparently encrypts/decrypts (Fernet).

Use it for any PII at rest: ``Mapped[str] = mapped_column(EncryptedString)``.
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

from kith.core.crypto import default_cipher


class EncryptedString(TypeDecorator):
    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:  # noqa: ANN001
        return None if value is None else default_cipher().encrypt(value)

    def process_result_value(self, value: str | None, dialect) -> str | None:  # noqa: ANN001
        return None if value is None else default_cipher().decrypt(value)
