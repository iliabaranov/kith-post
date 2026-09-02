"""Receipts and STOP replies pushed back by an SMS provider.

Like the WhatsApp webhook, these are the only endpoints a machine talks to, so
they are authenticated by signature rather than by a session cookie, and anything
that fails the check is refused without being looked at.

**Two providers, two signature schemes, two endpoints.** This is the whole
reason there is more than one route here:

* ``/sms/webhook/twilio`` — Twilio signs a base64 HMAC-SHA1, keyed with the
  account auth token, over the callback URL plus every POST parameter sorted and
  concatenated. The body itself is not what is signed.
* ``/sms/webhook/gateway`` — the capcom6 gateway signs a hex HMAC-SHA256 over the
  raw body plus a timestamp header, keyed with the shared webhook secret.

Checking either with the other's scheme would reject every legitimate request,
so they never share a handler and each calls only its own verifier. What they do
share is the normalised shape below, so the two things we actually care about —
a delivery receipt and an opt-out — are handled once.

**Why a receipt is not "Opened".** Opened means a person loaded the invitation
page. Delivered is the carrier's fact about the message, so it lives in its own
column and is shown as its own thing, exactly as the WhatsApp receipts are. SMS
has no read receipt at all, so there is nothing else on offer here and no
temptation to invent one.

**STOP is not optional.** A number that replies STOP is recorded on the contact,
not just the recipient, because an opt-out is permanent and applies to every
future card. Enforcement lives on the send paths; this endpoint only records it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from kith.config import get_settings
from kith.core import phones
from kith.db.models import Contact, Recipient
from kith.services import contacts as book
from kith.services import sms
from kith.services import sms_twilio as twilio
from kith.web.deps import get_db
from kith.web.ratelimit import limiter

log = logging.getLogger("kith")
router = APIRouter()

# A receipt is a few hundred bytes. Anything larger is not one, and must be
# refused before it is buffered and hashed — these endpoints are reachable from
# the internet by anyone who finds them, signature or not.
MAX_WEBHOOK_BODY = 64 * 1024


def _ok(detail: str = "ok") -> JSONResponse:
    # Always 200 once authenticated: both providers retry on failure, and we
    # would rather drop a receipt we don't understand than have it redelivered
    # fifteen times.
    return JSONResponse({"status": detail})


def _too_large() -> JSONResponse:
    return JSONResponse({"error": "body too large"}, status_code=413)


@dataclass(frozen=True)
class _Receipt:
    """A delivery status, normalised out of whichever provider reported it."""

    message_id: str
    delivered: bool
    failed: bool


@dataclass(frozen=True)
class _Inbound:
    """A message someone sent *to* us — which is how an opt-out arrives."""

    sender: str
    body: str


async def _read_capped(request: Request) -> bytes | None:
    """The raw body, or None if it is too big to be a receipt."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_WEBHOOK_BODY:
        return None
    raw = await request.body()
    return None if len(raw) > MAX_WEBHOOK_BODY else raw


# --- Twilio -------------------------------------------------------------------

@router.post("/sms/webhook/twilio")
@limiter.limit("240/minute")
async def twilio_webhook(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    if not settings.sms_webhooks_configured:
        return JSONResponse({"error": "receipts are not enabled"}, status_code=404)

    raw = await _read_capped(request)
    if raw is None:
        return _too_large()

    # Twilio posts form-encoded, and the form values are the signed material.
    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "not a form post"}, status_code=400)
    params = {k: str(v) for k, v in form.items()}

    # The configured callback URL, not the URL as this request arrived: a
    # tunnel or proxy rewrites host and scheme, and the signature is over what
    # Twilio was told to call.
    if not twilio.verify_twilio_signature(
        settings.sms_twilio_auth_token,
        settings.sms_status_callback_url,
        params,
        request.headers.get(twilio.TWILIO_SIGNATURE_HEADER),
    ):
        # Debug, not warning: an internet-reachable endpoint attracts unsigned
        # traffic, and a log line per attempt is a flooding vector of its own.
        log.debug("sms webhook: rejected an unsigned or mis-signed Twilio POST")
        return JSONResponse({"error": "bad signature"}, status_code=401)

    # One endpoint, both event kinds: Twilio posts a status callback and an
    # inbound message to whichever URL each is configured with, and an operator
    # may well point both here. They are told apart by which fields are present.
    status = (params.get("MessageStatus") or params.get("SmsStatus") or "").lower()
    if status:
        sid = params.get("MessageSid") or params.get("SmsSid") or ""
        return _record_receipt(db, _Receipt(
            message_id=sid,
            delivered=status in twilio.TWILIO_DELIVERED,
            failed=status in twilio.TWILIO_FAILED,
        ), provider="twilio")
    if params.get("Body") is not None and params.get("From"):
        return _record_inbound(db, _Inbound(
            sender=params.get("From", ""), body=params.get("Body", "")
        ), provider="twilio")
    return _ok("nothing to record")


# --- capcom6 gateway ----------------------------------------------------------

# The gateway's event names. Delivery is the only status worth a column; a
# failure is logged rather than stored, since a send that failed at the carrier
# leaves the recipient marked 'sent' either way and re-deriving that is Phase 6
# territory at best.
GATEWAY_DELIVERED = "sms:delivered"
GATEWAY_FAILED = frozenset({"sms:failed", "sms:cancelled"})
GATEWAY_RECEIVED = "sms:received"


@router.post("/sms/webhook/gateway")
@limiter.limit("240/minute")
async def gateway_webhook(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    if not settings.sms_webhooks_configured:
        return JSONResponse({"error": "receipts are not enabled"}, status_code=404)

    raw = await _read_capped(request)
    if raw is None:
        return _too_large()

    if not sms.verify_gateway_webhook(
        settings.sms_webhook_secret,
        raw,
        request.headers.get(sms.GATEWAY_SIGNATURE_HEADER),
        request.headers.get(sms.GATEWAY_TIMESTAMP_HEADER),
    ):
        log.debug("sms webhook: rejected an unsigned, mis-signed or stale gateway POST")
        return JSONResponse({"error": "bad signature"}, status_code=401)

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "not json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "not an object"}, status_code=400)

    event = str(body.get("event") or "")
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    payload = payload or {}

    if event == GATEWAY_DELIVERED or event in GATEWAY_FAILED:
        return _record_receipt(db, _Receipt(
            message_id=str(payload.get("messageId") or ""),
            delivered=event == GATEWAY_DELIVERED,
            failed=event in GATEWAY_FAILED,
        ), provider="gateway")
    if event == GATEWAY_RECEIVED:
        return _record_inbound(db, _Inbound(
            sender=str(payload.get("sender") or ""),
            body=str(payload.get("message") or ""),
        ), provider="gateway")
    return _ok(f"ignored {event or 'unknown event'}")


# --- what the two of them share -----------------------------------------------

def _record_receipt(db: Session, receipt: _Receipt, *, provider: str) -> JSONResponse:
    if not receipt.message_id:
        return _ok("nothing to record")
    # Matched on the provider's message id alone. The WhatsApp ack scopes its
    # match to the reporting session to stop one account stamping a receipt onto
    # another's recipient; SMS has no equivalent, because the channel is
    # instance-level — one provider account for the whole site, so a callback
    # carries no per-user identity to scope by. The id is opaque and
    # provider-generated, and reaching this line already required a valid
    # signature.
    r = db.execute(
        select(Recipient).where(Recipient.sms_message_id == receipt.message_id)
    ).scalars().first()
    if r is None:
        # Ordinary enough: receipts arrive for messages sent before a database
        # reset, or for another instance sharing the provider account.
        return _ok("not one of ours")
    if receipt.failed:
        log.warning(
            "sms webhook: %s reported a delivery failure for recipient %s",
            provider, r.id,
        )
        return _ok("failure logged")
    if receipt.delivered and r.sms_delivered_at is None:
        # Set once. Receipts repeat and arrive out of order, and the first
        # confirmation is the honest timestamp.
        r.sms_delivered_at = datetime.now(UTC)
        db.commit()
    return _ok("delivered")


def _record_inbound(db: Session, inbound: _Inbound, *, provider: str) -> JSONResponse:
    """Honour an opt-out. Everything else someone texts us is none of our business."""
    intent = sms.opt_out_intent(inbound.body)
    if intent is None:
        # Deliberately not stored, not forwarded, not shown to the host. A reply
        # to an invitation belongs in the conversation the host is already
        # having, not in a database column they never asked for.
        return _ok("no opt-out keyword")

    number = phones.normalize(inbound.sender)
    if not number:
        log.warning("sms webhook: %s sent an opt-out from an unparseable number", provider)
        return _ok("unparseable sender")

    opted_out = intent == "stop"

    # Per-contact first: this is the durable half, and it is what stops the
    # number being texted on some future card it isn't a recipient of yet.
    contacts = db.execute(
        select(Contact).where(Contact.phone_hash == book.phone_hash(number))
    ).scalars().all()
    for c in contacts:
        c.opted_out_sms = opted_out

    # ...and every recipient row carrying that number. Two reasons: a card
    # mid-send stops immediately, and these rows are what let the send paths
    # recognise the number again on a *future* card. A number that texted STOP
    # was necessarily messaged before, so it always has at least one recipient
    # row — which closes the gap left by matching contacts alone, since a number
    # typed straight into the compose box never became a contact.
    #
    # Matched in Python because `phone` is encrypted and, unlike Contact.phone,
    # carries no blind index of its own.
    rows = db.execute(
        select(Recipient).where(Recipient.channel == "sms")
    ).scalars().all()
    touched = [r for r in rows if r.phone == number]
    for r in touched:
        r.opted_out = opted_out
    db.commit()

    log.info(
        "sms webhook: %s reported %s from a number matching %d contact(s) and "
        "%d recipient(s)",
        provider, intent.upper(), len(contacts), len(touched),
    )
    return _ok(intent)
