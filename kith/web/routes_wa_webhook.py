"""Receipts pushed back from WAHA: delivery, read, and session status.

This is the only endpoint in the app that a machine talks to, so it is the only
one authenticated by a shared secret rather than a session cookie: WAHA signs
each POST with an HMAC-SHA512 of the exact body (verified against a live 2026.8.1
container), and anything that fails that check is refused without being looked at.

**Why receipts are not "Opened".** Opened means a person loaded the invitation
page, and this product's whole tracking story rests on that being true. Delivered
and read are WhatsApp's own facts about the message — the same ticks the host can
already see in their phone — so they're recorded in their own columns and shown as
their own thing. A read receipt is not a page visit, and conflating them would
undo the honesty the no-pixel design is for. (Recipients can also switch read
receipts off, so their absence means nothing at all.)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from kith.config import get_settings
from kith.db.models import Recipient, User
from kith.services import waha
from kith.web.deps import get_db

log = logging.getLogger("kith")
router = APIRouter()


def _ok(detail: str = "ok") -> JSONResponse:
    # Always 200 once authenticated: WAHA retries on failure, and we would rather
    # drop a receipt we don't understand than have it redelivered fifteen times.
    return JSONResponse({"status": detail})


@router.post("/wa/webhook")
async def wa_webhook(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    if not settings.waha_webhooks_configured:
        return JSONResponse({"error": "receipts are not enabled"}, status_code=404)

    raw = await request.body()
    if not waha.verify_webhook(
        settings.waha_webhook_secret, raw, request.headers.get(waha.WEBHOOK_HMAC_HEADER)
    ):
        log.warning("wa webhook: rejected an unsigned or mis-signed POST")
        return JSONResponse({"error": "bad signature"}, status_code=401)

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "not json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "not an object"}, status_code=400)

    event = str(body.get("event") or "")
    session = str(body.get("session") or "")
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}

    if event == "session.status":
        return _session_status(db, session, payload or {})
    if event == "message.ack":
        return _message_ack(db, session, payload or {})
    return _ok(f"ignored {event or 'unknown event'}")


def _session_status(db: Session, session: str, payload: dict) -> JSONResponse:
    """Cache a status change the moment WhatsApp reports it.

    Worth having even though every page refresh re-reads the session: this is how
    the dashboard warns about a link that dropped *between* visits.
    """
    status = str(payload.get("status") or "")
    if not session or not status:
        return _ok("nothing to record")
    user = db.execute(
        select(User).where(User.wa_session == session)
    ).scalar_one_or_none()
    if user is None:
        return _ok("no such session here")
    user.wa_status = status
    user.wa_status_at = datetime.now(UTC)
    db.commit()
    log.info("wa webhook: session %s -> %s", session, status)
    return _ok(f"status {status}")


def _message_ack(db: Session, session: str, payload: dict) -> JSONResponse:
    """Record WhatsApp's delivery/read receipt for a message we sent."""
    message_id = str(payload.get("id") or "")
    ack = payload.get("ack")
    if not message_id or not isinstance(ack, int | float):
        return _ok("nothing to record")
    ack = int(ack)
    r = db.execute(
        select(Recipient).where(Recipient.wa_message_id == message_id)
    ).scalar_one_or_none()
    if r is None:
        # Ordinary: acks arrive for the host's own unrelated conversations too.
        return _ok("not one of ours")

    now = datetime.now(UTC)
    # Acks can arrive out of order and can repeat; only ever move forwards.
    if r.wa_ack is None or ack > r.wa_ack:
        r.wa_ack = ack
    if ack >= waha.ACK_DEVICE and r.wa_delivered_at is None:
        r.wa_delivered_at = now
    if ack >= waha.ACK_READ and r.wa_read_at is None:
        r.wa_read_at = now
    if ack == waha.ACK_ERROR:
        log.warning(
            "wa webhook: WhatsApp reported a failure for recipient %s (%s)",
            r.id, payload.get("ackName"),
        )
    db.commit()
    return _ok(f"ack {ack}")
