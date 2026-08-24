"""Address book: a user's reusable contacts.

Dedup and lookup go through a blind index (keyed HMAC of the normalized value),
so we never store or query plaintext — the email/phone/name columns stay Fernet-
encrypted. Importing into an event copies contacts into Recipient rows, so per-
event edits never touch the book.

A contact can be reachable by email, by WhatsApp number, or both. Identity — what
makes two entries the same person — is the email when there is one, else the
number; see :func:`identity_hash`.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from kith.core.crypto import default_cipher
from kith.core.recipients import (
    Parsed,
    identity_of,
    parse_mixed,
    parse_phones,
    parse_recipients,
)
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


def identity_hash(email: str | None, phone: str | None = None) -> str:
    """Blind index of a contact's identity: the email, else "tel:<e164>".

    Stored in ``Contact.email_hash``, which is NOT NULL and carries the per-user
    UNIQUE constraint. Folding the phone into it is what keeps WhatsApp-only
    contacts distinct from one another — they all share ``email == ""``, so
    hashing the email alone would collapse them into a single row.
    """
    return default_cipher().blind_index(identity_of(email, phone))


def phone_hash(phone: str) -> str:
    """Blind index of a phone number, for "do I already have this number?"."""
    return default_cipher().blind_index(phone)


def list_contacts(db: Session, user_id: str) -> list[Contact]:
    """All of a user's contacts, most-recently-used first."""
    rows = db.execute(select(Contact).where(Contact.user_id == user_id)).scalars().all()
    return sorted(rows, key=lambda c: (c.last_used_at or c.created_at), reverse=True)


def find_by_email(db: Session, user_id: str, email: str) -> Contact | None:
    return db.execute(
        select(Contact).where(Contact.user_id == user_id, Contact.email_hash == _hash(email))
    ).scalar_one_or_none()


def find_by_identity(db: Session, user_id: str, email: str | None, phone: str | None) -> (
    Contact | None
):
    """The contact this (email, phone) pair *is*, if the book already has them."""
    return db.execute(
        select(Contact).where(
            Contact.user_id == user_id,
            Contact.email_hash == identity_hash(email, phone),
        )
    ).scalar_one_or_none()


def find_by_phone(db: Session, user_id: str, phone: str) -> Contact | None:
    """Whoever holds this number, whether or not they also have an email."""
    return db.execute(
        select(Contact).where(
            Contact.user_id == user_id, Contact.phone_hash == phone_hash(phone)
        )
    ).scalars().first()


def _parse_one(email: str | None, phone: str | None, name: str | None) -> Parsed | None:
    """Validate an (email, phone, name) triple into a single Parsed, or None.

    Either address is enough. When both are given the email carries the identity
    and the phone rides along, which is why the email is parsed first.
    """
    label = f"{name} <{{}}>" if name else "{}"
    if email and email.strip():
        parsed, _ = parse_recipients(label.format(email))
        if not parsed:
            return None
        e164 = None
        if phone and phone.strip():
            ph, _ = parse_phones(phone)
            if not ph:
                return None  # a number was offered and it's unusable — say so
            e164 = ph[0].phone
        return Parsed(name=parsed[0].name, email=parsed[0].email, phone=e164)
    if phone and phone.strip():
        parsed, _ = parse_phones(label.format(phone))
        return parsed[0] if parsed else None
    return None


def add_contact(
    db: Session, user_id: str, email: str, name: str | None = None,
    groups: list[str] | None = None, phone: str | None = None,
) -> tuple[Contact | None, bool]:
    """Add a contact; if this person is already in the book, return them instead.
    Returns (contact, created?). Nothing usable to reach them by → (None, False)."""
    p = _parse_one(email, phone, name)
    if p is None:
        return None, False
    existing = find_by_identity(db, user_id, p.email or None, p.phone)
    if existing is not None:
        changed = False
        if p.name and not existing.name:  # fill in a missing name, don't overwrite
            existing.name = p.name
            changed = True
        if p.phone and not existing.phone:  # adding a number to a known contact
            existing.phone = p.phone
            existing.phone_hash = phone_hash(p.phone)
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
        user_id=user_id,
        email=p.email,  # "" for a WhatsApp-only contact; the column is NOT NULL
        name=p.name,
        email_hash=identity_hash(p.email or None, p.phone),
        phone=p.phone,
        phone_hash=phone_hash(p.phone) if p.phone else None,
        groups=groups or [],
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c, True


def import_text(db: Session, user_id: str, text: str) -> tuple[int, int, list[str]]:
    """Bulk-add from pasted text. Emails, WhatsApp numbers, or a mix of both.
    Returns (added, skipped, invalid)."""
    parsed, invalid = parse_mixed(text)
    added = skipped = 0
    for p in parsed:
        _, created = add_contact(db, user_id, p.email, p.name, phone=p.phone)
        if created:
            added += 1
        else:
            skipped += 1
    return added, skipped, invalid


CSV_TEMPLATE = (
    "name,email,phone,groups\n"
    'Alex Rivera,alex@example.com,,"family, local"\n'
    "Sam Chen,sam@example.com,+15551234567,work\n"
    "Jordan Lee,,+14085559090,\n"
    "Robin Ng,robin@example.com,,\n"
)

# Column names we understand in a CSV header, mapped to their meaning. Anything
# else in the header is ignored.
_CSV_ALIASES = {
    "name": "name", "full name": "name", "contact": "name",
    "email": "email", "e-mail": "email", "email address": "email", "mail": "email",
    "phone": "phone", "whatsapp": "phone", "number": "phone",
    "phone number": "phone", "whatsapp number": "phone", "mobile": "phone",
    "groups": "groups", "group": "groups", "tags": "groups", "tag": "groups",
}


def _csv_header_map(cells: list[str]) -> dict[str, int] | None:
    """Map a header row to column indexes, or None if this isn't a header.

    Header-driven mapping is what lets a phone column exist at all: positionally,
    a third column has always meant *groups*, so reading it as a phone would
    silently mangle every CSV anyone already has.
    """
    mapping: dict[str, int] = {}
    for i, cell in enumerate(cells):
        key = _CSV_ALIASES.get(cell.strip().lower())
        if key and key not in mapping:
            mapping[key] = i
    # A real header names at least one address column; a data row starting with a
    # name would not.
    return mapping if ("email" in mapping or "phone" in mapping) else None


def _csv_row(
    cells: list[str], header: dict[str, int] | None
) -> tuple[str, str, str, list[str]]:
    """One CSV row -> (name, email, phone, groups), by header or by position."""
    if header is not None:
        def col(key: str) -> str:
            idx = header.get(key, -1)
            return cells[idx] if 0 <= idx < len(cells) else ""

        return col("name"), col("email"), col("phone"), parse_groups(col("groups"))
    if len(cells) >= 2:
        return cells[0], cells[1], "", parse_groups(",".join(cells[2:]))
    return "", cells[0], "", []


def import_csv(db: Session, user_id: str, text: str) -> tuple[int, int, list[str]]:
    """Bulk-add from a CSV. Returns (added, skipped, invalid).

    Two shapes are accepted:

    * **with a header** — columns are matched by name, in any order, and may
      include ``phone``: ``name, email, phone, groups``. This is what the
      downloadable template uses.
    * **without one** — the original positional shape, ``name, email`` plus any
      further columns as group tags. Kept exactly as it was, because a third
      column has always meant groups and reinterpreting it as a phone would
      quietly mangle files people already have.

    A groups cell may hold several comma-separated tags (quote it in the file).
    """
    added = skipped = 0
    invalid: list[str] = []
    header: dict[str, int] | None = None
    for i, row in enumerate(csv.reader(io.StringIO(text))):
        cells = [c.strip() for c in row]
        if not any(cells):
            continue
        if i == 0:
            header = _csv_header_map(cells)
            if header is not None:
                continue

        name, email, phone, groups = _csv_row(cells, header)
        contact, created = add_contact(
            db, user_id, email, name or None, groups=groups, phone=phone or None
        )
        if contact is None:
            invalid.append(",".join(cells) or "(blank)")
        elif created:
            added += 1
        else:
            skipped += 1
    return added, skipped, invalid


def update_contact(
    db: Session, user_id: str, contact_id: str, email: str, name: str | None,
    groups: list[str] | None = None, phone: str | None = None,
) -> Contact | None:
    """Edit a contact. Returns None if not found/owned, if there's nothing usable
    to reach them by, or if the edit would collide with a different existing
    contact. When groups is not None it replaces the contact's tags."""
    c = db.get(Contact, contact_id)
    if c is None or c.user_id != user_id:
        return None
    p = _parse_one(email, phone, name)
    if p is None:
        return None
    clash = find_by_identity(db, user_id, p.email or None, p.phone)
    if clash is not None and clash.id != c.id:
        return None
    c.email = p.email
    c.email_hash = identity_hash(p.email or None, p.phone)
    c.phone = p.phone
    c.phone_hash = phone_hash(p.phone) if p.phone else None
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


def mark_used(
    db: Session, user_id: str, emails: list[str], phones: list[str] | None = None
) -> None:
    """Bump last_used_at for contacts an event imported (for recency sorting)."""
    now = datetime.now(UTC)
    hashes = {_hash(e) for e in emails}
    hashes |= {identity_hash(None, ph) for ph in (phones or [])}
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
        h = identity_hash(p.email or None, p.phone)
        if h in known or h in seen:
            continue
        seen.add(h)
        out.append(p)
    return out
