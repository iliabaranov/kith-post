"""Address book: a user's reusable contacts.

Dedup and lookup go through a blind index (keyed HMAC of the normalized email),
so we never store or query plaintext — the email/name columns stay Fernet-
encrypted. Importing into an event copies contacts into Recipient rows, so per-
event edits never touch the book.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from kith.core.crypto import default_cipher
from kith.core.recipients import Parsed, parse_recipients
from kith.db.models import Contact


def _norm(email: str) -> str:
    return email.strip().lower()


def parse_groups(text: str) -> list[str]:
    """Comma-separated tags → clean, de-duplicated list (case-insensitive dedup,
    original casing kept)."""
    out: list[str] = []
    seen: set[str] = set()
    for chunk in (text or "").split(","):
        g = chunk.strip()
        if g and g.lower() not in seen:
            seen.add(g.lower())
            out.append(g)
    return out


def all_groups(db: Session, user_id: str) -> list[str]:
    """Every distinct group tag across a user's contacts, sorted (case-insensitive)."""
    seen: dict[str, str] = {}
    for c in db.execute(select(Contact).where(Contact.user_id == user_id)).scalars():
        for g in (c.groups or []):
            seen.setdefault(g.lower(), g)
    return [seen[k] for k in sorted(seen)]


def _hash(email: str) -> str:
    return default_cipher().blind_index(_norm(email))


def list_contacts(db: Session, user_id: str) -> list[Contact]:
    """All of a user's contacts, most-recently-used first."""
    rows = db.execute(select(Contact).where(Contact.user_id == user_id)).scalars().all()
    return sorted(rows, key=lambda c: (c.last_used_at or c.created_at), reverse=True)


def find_by_email(db: Session, user_id: str, email: str) -> Contact | None:
    return db.execute(
        select(Contact).where(Contact.user_id == user_id, Contact.email_hash == _hash(email))
    ).scalar_one_or_none()


def add_contact(
    db: Session, user_id: str, email: str, name: str | None = None,
    groups: list[str] | None = None,
) -> tuple[Contact | None, bool]:
    """Add a contact; if one with this email already exists, return it instead.
    Returns (contact, created?). A blank/invalid email yields (None, False)."""
    parsed, _ = parse_recipients(email if name is None else f"{name} <{email}>")
    if not parsed:
        return None, False
    p = parsed[0]
    existing = find_by_email(db, user_id, p.email)
    if existing is not None:
        changed = False
        if p.name and not existing.name:  # fill in a missing name, don't overwrite
            existing.name = p.name
            changed = True
        if groups:  # union any new tags into the existing set
            merged = list(existing.groups or [])
            have = {g.lower() for g in merged}
            for g in groups:
                if g.lower() not in have:
                    merged.append(g)
                    have.add(g.lower())
            existing.groups = merged
            changed = True
        if changed:
            db.commit()
        return existing, False
    c = Contact(
        user_id=user_id, email=p.email, name=p.name, email_hash=_hash(p.email),
        groups=groups or [],
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c, True


def import_text(db: Session, user_id: str, text: str) -> tuple[int, int, list[str]]:
    """Bulk-add from pasted text / CSV lines. Returns (added, skipped, invalid)."""
    parsed, invalid = parse_recipients(text)
    added = skipped = 0
    for p in parsed:
        _, created = add_contact(db, user_id, p.email, p.name)
        if created:
            added += 1
        else:
            skipped += 1
    return added, skipped, invalid


CSV_TEMPLATE = (
    "name,email,groups\n"
    'Alex Rivera,alex@example.com,"family, local"\n'
    "Sam Chen,sam@example.com,work\n"
    "Jordan Lee,jordan@example.com,\n"
)


def import_csv(db: Session, user_id: str, text: str) -> tuple[int, int, list[str]]:
    """Bulk-add from a CSV with columns: name, email, groups. The groups cell may
    hold several comma-separated tags (quote it in the file), and any extra trailing
    columns are also treated as tags. Returns (added, skipped, invalid)."""
    added = skipped = 0
    invalid: list[str] = []
    for i, row in enumerate(csv.reader(io.StringIO(text))):
        cells = [c.strip() for c in row]
        if not any(cells):
            continue
        # skip a header row
        is_header = cells[0].lower() == "name" or (len(cells) > 1 and cells[1].lower() == "email")
        if i == 0 and is_header:
            continue
        if len(cells) >= 2:
            name, email, groups = cells[0], cells[1], parse_groups(",".join(cells[2:]))
        else:
            name, email, groups = "", cells[0], []
        contact, created = add_contact(db, user_id, email, name or None, groups=groups)
        if contact is None:
            invalid.append(",".join(cells) or "(blank)")
        elif created:
            added += 1
        else:
            skipped += 1
    return added, skipped, invalid


def update_contact(
    db: Session, user_id: str, contact_id: str, email: str, name: str | None,
    groups: list[str] | None = None,
) -> Contact | None:
    """Edit a contact. Returns None if not found/owned, or if the new email
    would collide with a different existing contact. When groups is not None it
    replaces the contact's tags."""
    c = db.get(Contact, contact_id)
    if c is None or c.user_id != user_id:
        return None
    parsed, _ = parse_recipients(f"{name} <{email}>" if name else email)
    if not parsed:
        return None
    p = parsed[0]
    clash = find_by_email(db, user_id, p.email)
    if clash is not None and clash.id != c.id:
        return None
    c.email = p.email
    c.email_hash = _hash(p.email)
    c.name = p.name
    if groups is not None:
        c.groups = groups
    db.commit()
    return c


def delete_contact(db: Session, user_id: str, contact_id: str) -> bool:
    c = db.get(Contact, contact_id)
    if c is None or c.user_id != user_id:
        return False
    db.delete(c)
    db.commit()
    return True


def mark_used(db: Session, user_id: str, emails: list[str]) -> None:
    """Bump last_used_at for contacts an event imported (for recency sorting)."""
    now = datetime.now(UTC)
    hashes = {_hash(e) for e in emails}
    for c in db.execute(select(Contact).where(Contact.user_id == user_id)).scalars():
        if c.email_hash in hashes:
            c.last_used_at = now
    db.commit()


def new_among(db: Session, user_id: str, parsed: list[Parsed]) -> list[Parsed]:
    """From a parsed recipient list, the people NOT already in the book (deduped)
    — drives the 'add these new people?' prompt after an event is created."""
    known = {c.email_hash for c in list_contacts(db, user_id)}
    out: list[Parsed] = []
    seen: set[str] = set()
    for p in parsed:
        h = _hash(p.email)
        if h in known or h in seen:
            continue
        seen.add(h)
        out.append(p)
    return out
