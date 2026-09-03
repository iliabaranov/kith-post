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
so they never share a handler and each calls only its own verifier — and each
exists only while its provider is the configured one, since an endpoint whose
signature can never be satisfied is better off not answering at all. What they
do share is the normalised shape below, so the two things we actually care
about — a delivery receipt and an opt-out — are handled once.

**Why a receipt is not "Opened".** Opened means a person loaded the invitation
page. Delivered is the carrier's fact about the message, so it lives in its own
column and is shown as its own thing, exactly as the WhatsApp receipts are. SMS
has no read receipt at all, so there is nothing else on offer here and no
temptation to invent one.

**STOP is not optional.** A number that replies STOP is recorded, by its blind
index, in a log of its own rather than as a flag on the contact or recipient it
happened to be on: an opt-out is permanent and applies to every future card, so
it has to outlive the event it arrived on and the address-book entry it might
be deleted with. Enforcement lives on the send paths; this endpoint only records.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kith.config import get_settings
from kith.core import phones
from kith.db.models import Contact, Recipient, SmsOptOutEvent
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

# Twilio marks a message that arrived *to* us with one of these, in the same
# SmsStatus field a status callback uses for queued/sent/delivered. The two
# kinds of POST are told apart by this value, never by whether the field exists.
TWILIO_INBOUND_STATUSES = frozenset({"", "received", "receiving"})


def _ok(detail: str = "ok") -> JSONResponse:
    # Always 200 once authenticated: both providers retry on failure, and we
    # would rather drop a receipt we don't understand than have it redelivered
    # fifteen times.
    return JSONResponse({"status": detail})


def _too_large() -> JSONResponse:
    return JSONResponse({"error": "body too large"}, status_code=413)


def _not_here() -> JSONResponse:
    return JSONResponse({"error": "receipts are not enabled"}, status_code=404)


def _twiml(outcome: str) -> Response:
    """The answer Twilio wants to an *inbound message*: TwiML, even if empty.

    A JSON body here earns an "Invalid Content-Type" (12300) in the Twilio
    console for every STOP and every ordinary reply. An empty <Response/> means
    "nothing to say back", which is exactly right — the reply belongs to the
    conversation the host is having, not to us. The outcome rides in a header
    for the tests; Twilio ignores it.
    """
    return Response(
        "<Response/>", media_type="text/xml", headers={"X-Kith-Outcome": outcome},
    )


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
    message_id: str = ""     # the provider's id for this message; dedups a replay
    reference: str = ""      # our own number, as the provider saw it (see _sender_e164)


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
    # Gated on the provider as well as the secret: with the gateway configured
    # there is no auth token for this endpoint to verify against.
    if not settings.sms_webhooks_configured or settings.sms_provider != "twilio":
        return _not_here()

    raw = await _read_capped(request)
    if raw is None:
        return _too_large()

    # Twilio posts form-encoded, and the form values are the signed material.
    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "not a form post"}, status_code=400)
    # A repeated key collapses to its last value. Twilio does not document
    # duplicate parameters and its own validator sorts a flat dict, so this is
    # what it signs; it posts urlencoded forms, never multipart files.
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
    # may well point both here. They are told apart by the status VALUE: a real
    # inbound message arrives with SmsStatus=received alongside From and Body.
    # Dispatching on whether a status field was present filed every STOP as a
    # delivery receipt and opted nobody out.
    status = (params.get("MessageStatus") or params.get("SmsStatus") or "").lower()
    sid = params.get("MessageSid") or params.get("SmsSid") or ""
    if (
        params.get("Body") is not None
        and params.get("From")
        and status in TWILIO_INBOUND_STATUSES
    ):
        outcome = _record_inbound(db, _Inbound(
            sender=params.get("From", ""), body=params.get("Body", ""),
            message_id=sid, reference=params.get("To", ""),
        ), provider="twilio")
        return _twiml(outcome)
    if status:
        return _record_receipt(db, _Receipt(
            message_id=sid,
            delivered=status in twilio.TWILIO_DELIVERED,
            failed=status in twilio.TWILIO_FAILED,
        ), provider="twilio")
    return _ok("nothing to record")


# --- capcom6 gateway ----------------------------------------------------------

# The gateway's event names. Delivery and failure each get a column; an inbound
# message is only ever looked at for an opt-out keyword.
GATEWAY_DELIVERED = "sms:delivered"
GATEWAY_FAILED = frozenset({"sms:failed", "sms:cancelled"})
GATEWAY_RECEIVED = "sms:received"


@router.post("/sms/webhook/gateway")
@limiter.limit("240/minute")
async def gateway_webhook(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    # Gated on the provider as well as the secret: on a Twilio box this secret
    # is a switch, not a key, and an endpoint it alone unlocked could forge a
    # STOP — or a START — for any number on the site.
    if not settings.sms_webhooks_configured or settings.sms_provider != "gateway":
        return _not_here()

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
        return _ok(_record_inbound(db, _Inbound(
            sender=str(payload.get("sender") or ""),
            body=str(payload.get("message") or ""),
            message_id=str(payload.get("messageId") or ""),
            reference=str(payload.get("recipient") or ""),
        ), provider="gateway"))
    # Not reflected verbatim: it is caller-controlled, so it is capped.
    return _ok(f"ignored {event[:40] or 'unknown event'}")


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
        # The carrier refused it. Stored, not just logged: this is the host's
        # only signal that a number is bad, and without it a text that never
        # arrived reads "sent" for ever.
        if r.sms_failed_at is None:
            r.sms_failed_at = datetime.now(UTC)
            db.commit()
        log.warning(
            "sms webhook: %s reported a delivery failure for recipient %s",
            provider, r.id,
        )
        return _ok("failure recorded")
    if receipt.delivered and r.sms_delivered_at is None:
        # Set once. Receipts repeat and arrive out of order, and the first
        # confirmation is the honest timestamp.
        r.sms_delivered_at = datetime.now(UTC)
        db.commit()
    return _ok("delivered")


def _sender_e164(db: Session, inbound: _Inbound) -> str | None:
    """The sender's number in E.164, or None if it cannot be pinned down.

    Twilio reports E.164 and the common case is one call to ``normalize``. The
    gateway reports what the handset saw, which for a domestic sender is often
    the national number with no country code — and dropping a STOP because it
    arrived as "6505551212" would be the worst possible reading of "we don't
    guess countries". So the country code is borrowed from *our own* number,
    which the same POST carries, and only accepted when the result is
    unambiguous: it matches a number in someone's address book, or it is the
    one North American shape (+1 and exactly ten digits), or it is the only
    candidate that parses at all. Anything else is refused and logged.
    """
    number = phones.normalize(inbound.sender)
    if number:
        return number
    national = re.sub(r"\D", "", inbound.sender or "")
    ours = phones.normalize(inbound.reference)
    if not national or not ours:
        return None
    candidates: list[str] = []
    for k in (1, 2, 3):
        cand = phones.normalize(f"+{ours[1:1 + k]}{national}")
        if cand and cand not in candidates:
            candidates.append(cand)
    if not candidates:
        return None
    by_hash = {book.phone_hash(c): c for c in candidates}
    known = {
        h for h in db.execute(
            select(Contact.phone_hash).where(Contact.phone_hash.in_(list(by_hash)))
        ).scalars().all()
        if h
    }
    if len(known) == 1:
        return by_hash[known.pop()]
    if ours.startswith("+1") and len(national) == 10:
        return f"+1{national}"
    return candidates[0] if len(candidates) == 1 else None


def _record_inbound(db: Session, inbound: _Inbound, *, provider: str) -> str:
    """Honour an opt-out. Everything else someone texts us is none of our business."""
    intent = sms.opt_out_intent(inbound.body)
    if intent is None:
        # Deliberately not stored, not forwarded, not shown to the host. A reply
        # to an invitation belongs in the conversation the host is already
        # having, not in a database column they never asked for.
        return "no opt-out keyword"

    number = _sender_e164(db, inbound)
    if not number:
        log.warning(
            "sms webhook: %s sent an opt-out from a number that could not be "
            "resolved to E.164", provider,
        )
        return "unparseable sender"

    sid = inbound.message_id or None
    if sid and db.execute(
        select(SmsOptOutEvent.id).where(SmsOptOutEvent.message_sid == sid)
    ).first():
        # The same message, delivered again. Twilio's signature carries no
        # timestamp, so a captured POST could otherwise be replayed at any
        # later date to reverse whatever the person decided since.
        return "already recorded"

    # Appended, never updated: the current answer for a number is its latest
    # event, and the history is what makes a replayed START harmless.
    db.add(SmsOptOutEvent(
        phone_hash=book.phone_hash(number), kind=intent, source=provider, message_sid=sid,
    ))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()          # two copies of the same POST raced; one won
        return "already recorded"
    # Not the number, and not how many rows it touched: PII stays out of the log.
    log.info("sms webhook: %s reported %s", provider, intent.upper())
    return intent
