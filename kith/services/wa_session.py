"""The kith side of a user's WhatsApp link.

``services.waha`` talks to the container; this module owns what kith remembers
about it. The split matters because the credentials themselves live in WAHA's
volume and must never reach our database — all we keep is the session name, the
last status we saw, and enough about WhatsApp's restrictions to explain a pause
to the host.

The cached ``user.wa_*`` columns are for rendering pages. They are never trusted
at send time: :func:`sendable` re-reads the live session, because a pairing can
die between page loads and sending to a dead session is how you get silence
instead of an invitation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from kith.config import Settings
from kith.core import phones
from kith.db.models import User
from kith.services import waha

log = logging.getLogger("kith")


def session_name(user: User) -> str:
    """The WAHA session name for this user — derived, so it can't drift."""
    return phones.session_name(user.id)


def client(settings: Settings) -> waha.WahaClient:
    return waha.WahaClient.from_settings(settings)


def available(settings: Settings) -> bool:
    """Is the channel switched on for this deployment at all?"""
    return settings.whatsapp_configured


def linked(user: User) -> bool:
    """Has this host completed a pairing (as far as we last saw)?"""
    return bool(user.wa_session and user.wa_status == waha.STATUS_WORKING)


def acknowledged(user: User) -> bool:
    """Has the host accepted the "this can get your account banned" warning?"""
    return user.wa_risk_ack_at is not None


def acknowledge(db: Session, user: User) -> None:
    """Record the warning acknowledgement. No session is created before this."""
    if user.wa_risk_ack_at is None:
        user.wa_risk_ack_at = datetime.now(UTC)
        db.commit()


def _remember(db: Session, user: User, state: waha.SessionState | None) -> None:
    """Cache what WAHA just told us on the User row (for the UI only)."""
    if state is None:
        user.wa_status = None
        user.wa_status_at = datetime.now(UTC)
        db.commit()
        return
    was_working = user.wa_status == waha.STATUS_WORKING
    user.wa_session = state.name
    user.wa_status = state.status
    user.wa_status_at = datetime.now(UTC)
    if state.phone:
        user.wa_number = state.phone
    if state.is_working and not was_working:
        user.wa_linked_at = datetime.now(UTC)
    # Keep the restriction state where the UI can explain it. A timelock that has
    # lapsed is cleared, so an old pause doesn't keep scaring the host.
    tl = state.timelock
    user.wa_timelock_until = tl.ends_at if (tl and tl.is_active) else None
    cap = state.capping
    user.wa_capping = (
        {
            "status": cap.status,
            "total": cap.total,
            "used": cap.used,
            "cycle_end": cap.cycle_end.isoformat() if cap.cycle_end else None,
        }
        if cap
        else None
    )
    db.commit()


def refresh(db: Session, user: User, settings: Settings) -> waha.SessionState | None:
    """Read the live session and update the cache. None if WAHA has no session.

    Never raises for the ordinary "WAHA is unreachable" case: the linking page
    has to render something even when the container is down.
    """
    if not available(settings) or not user.wa_session:
        return None
    try:
        state = client(settings).find_session(user.wa_session)
    except waha.WahaError:
        log.exception("waha: could not read session for user %s", user.id)
        return None
    _remember(db, user, state)
    return state


def start_link(db: Session, user: User, settings: Settings) -> waha.SessionState:
    """Create/start this user's session so they can pair. Requires the ack.

    Idempotent: safe to call again on a half-finished attempt, and on a session
    that has drifted to FAILED (which an unpaired one does on its own).
    """
    if not available(settings):
        raise waha.WahaError("the WhatsApp channel is not enabled")
    if not acknowledged(user):
        raise waha.WahaError("the risk warning has not been acknowledged")
    name = session_name(user)
    c = client(settings)
    state = c.ensure_session(name)
    # A session made before receipts were configured has no webhooks, and WAHA
    # won't guess. Cheap and idempotent, so do it on every link attempt.
    try:
        if c.ensure_webhooks(name):
            state = c.get_session(name)
    except waha.WahaError:
        log.exception("waha: could not configure webhooks for %s", name)
    user.wa_session = name
    _remember(db, user, state)
    return state


def qr_png(user: User, settings: Settings) -> bytes:
    """The pairing QR, fetched server-side so the API key stays out of the browser."""
    if not user.wa_session:
        raise waha.WahaError("no session to pair")
    return client(settings).qr_png(user.wa_session)


def pairing_code(user: User, settings: Settings, phone_e164: str) -> str:
    """A code the host types into WhatsApp, for when they can't scan a QR.

    The number is used for this one request and deliberately not stored: it's the
    host's own WhatsApp number, we have no other use for it, and the pairing that
    results already tells us which account got linked.
    """
    if not user.wa_session:
        raise waha.WahaError("no session to pair")
    return client(settings).request_pairing_code(user.wa_session, phone_e164)


def unlink(db: Session, user: User, settings: Settings) -> None:
    """Drop the pairing and forget it.

    Best-effort against WAHA — if the container is down we still clear our side,
    because a host asking to unlink shouldn't be blocked by a service they can't
    see. The stored credentials are then cleaned up whenever WAHA is next
    reachable, or by removing its volume.
    """
    if user.wa_session and available(settings):
        try:
            client(settings).unlink(user.wa_session)
        except waha.WahaError:
            log.exception("waha: unlink failed for user %s; clearing locally", user.id)
    user.wa_session = None
    user.wa_status = None
    user.wa_status_at = datetime.now(UTC)
    user.wa_linked_at = None
    user.wa_number = None
    user.wa_timelock_until = None
    user.wa_capping = None
    db.commit()


def sendable(db: Session, user: User, settings: Settings) -> waha.SessionState:
    """The pre-flight every send goes through. Raises if we must not send.

    Deliberately re-reads the session rather than trusting ``user.wa_status``:
    the cache is a page-rendering convenience, and a pairing can die between a
    page load and a send.
    """
    if not available(settings):
        raise waha.NotLinked("the WhatsApp channel is not enabled")
    if not user.wa_session:
        raise waha.NotLinked("no WhatsApp account is linked")
    state = client(settings).get_session(user.wa_session)
    _remember(db, user, state)
    state.raise_if_unsendable()
    return state
