"""Parse a free-text recipient list — pure, side-effect-free.

Accepts emails separated by commas or newlines, optionally as "Name <email>".
Returns deduped, lower-cased valid entries plus the chunks that didn't parse, so
the UI can gently flag them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NAMED_RE = re.compile(r"^(.*?)<([^>]+)>$")


@dataclass(frozen=True)
class Parsed:
    name: str | None
    email: str


def normalize(email: str) -> str:
    """Canonical form for matching/dedup: trimmed + lower-cased."""
    return (email or "").strip().lower()


def parse_recipients(text: str) -> tuple[list[Parsed], list[str]]:
    valid: list[Parsed] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[,\n;]", text or ""):
        s = chunk.strip()
        if not s:
            continue
        name: str | None = None
        email = s
        m = _NAMED_RE.match(s)
        if m:
            name = (m.group(1).strip() or None)
            email = m.group(2).strip()
        email = normalize(email)
        if not _EMAIL_RE.match(email):
            invalid.append(s)
            continue
        if email in seen:
            continue
        seen.add(email)
        valid.append(Parsed(name=name, email=email))
    return valid, invalid
