"""A host's own texting setup — the SMS counterpart of the WhatsApp link page.

One page, ``/account/sms``, and four verbs: save, test, register the phone's
webhooks, remove. There is no pairing dance to poll, so this is plainer than
``routes_whatsapp``: a form, an honest status line, and a test button, because
a test text to your own phone is the only proof that credentials work.

Secrets never round-trip through the form. The password and token fields are
rendered empty with a "leave blank to keep" hint; ``services.sms_link.save``
treats blank as keep. Errors come back as ``error`` in the template context,
the way every other page here does it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from kith.config import get_settings
from kith.services import sms, sms_link
from kith.web.deps import get_db, load_user, templates
from kith.web.ratelimit import limiter

router = APIRouter()

# Copy for the flash-like notices carried in the query string, kept here so the
# template stays about layout.
_NOTICES = {
    "saved": "Saved. Send yourself a test text to make sure it works.",
    "tested": "Test text sent — check your phone.",
    "webhooks": "The phone will now report deliveries and STOP replies here.",
    "removed": "Your texting setup has been removed. Cards already sent keep working.",
}


def _page(
    request: Request, db: Session, user, *,  # noqa: ANN001 — a DB User
    error: str | None = None, notice: str | None = None, form: dict | None = None,
):
    settings = get_settings()
    link = sms_link.get(db, user)
    site = sms.SmsConfig.from_settings(settings)
    own = sms_link.config_from_link(link, settings) if link else None
    return templates.TemplateResponse(
        request,
        "sms.html",
        {
            "settings": settings,
            "user": user,
            "link": link,
            "own_ready": bool(own and own.configured),
            "site_ready": bool(site and site.configured),
            "site_provider": site.provider if site and site.configured else None,
            "webhook_url": sms_link.webhook_url(settings, link) if link else None,
            "error": error,
            "notice": notice,
            # What the form shows. After a failed save this is what they typed;
            # otherwise the stored non-secret values. Secrets are never in here.
            "form": form or _form_from(link),
        },
    )


def _form_from(link) -> dict:  # noqa: ANN001
    if link is None:
        return {"provider": "gateway", "gateway_path_relay": False}
    return {
        "provider": link.provider,
        "gateway_url": link.gateway_url or "",
        "gateway_user": link.gateway_user or "",
        "gateway_path_relay": link.gateway_path == sms_link.RELAY_PATH,
        "gateway_device_id": link.gateway_device_id or "",
        "gateway_encrypt": bool(link.gateway_passphrase),
        "twilio_account_sid": link.twilio_account_sid or "",
        "twilio_from": link.twilio_from or "",
        "twilio_messaging_service_sid": link.twilio_messaging_service_sid or "",
        "sender_number": link.sender_number or "",
        "self_number": link.self_number or "",
    }


def _gate(request: Request, db: Session):
    """The signed-in host, or the redirect to send instead."""
    user = load_user(request, db)
    if user is None:
        return None, RedirectResponse("/", status_code=303)
    if not sms_link.available(get_settings()):
        return None, RedirectResponse("/account", status_code=303)
    return user, None


@router.get("/account/sms", response_class=HTMLResponse)
def sms_page(request: Request, db: Session = Depends(get_db), notice: str = ""):
    user, redirect = _gate(request, db)
    if redirect is not None:
        return redirect
    return _page(request, db, user, notice=_NOTICES.get(notice))


@router.post("/account/sms", response_class=HTMLResponse)
@limiter.limit("30/minute")
def save(
    request: Request,
    db: Session = Depends(get_db),
    provider: str = Form(""),
    gateway_url: str = Form(""),
    gateway_user: str = Form(""),
    gateway_pass: str = Form(""),
    gateway_path_relay: str = Form(""),
    gateway_device_id: str = Form(""),
    gateway_encrypt: str = Form(""),
    gateway_passphrase: str = Form(""),
    twilio_account_sid: str = Form(""),
    twilio_auth_token: str = Form(""),
    twilio_from: str = Form(""),
    twilio_messaging_service_sid: str = Form(""),
    sender_number: str = Form(""),
    self_number: str = Form(""),
):
    user, redirect = _gate(request, db)
    if redirect is not None:
        return redirect
    try:
        sms_link.save(
            db, user,
            provider=provider,
            gateway_url=gateway_url, gateway_user=gateway_user, gateway_pass=gateway_pass,
            gateway_relay=bool(gateway_path_relay), gateway_device_id=gateway_device_id,
            gateway_encrypt=bool(gateway_encrypt), gateway_passphrase=gateway_passphrase,
            twilio_account_sid=twilio_account_sid, twilio_auth_token=twilio_auth_token,
            twilio_from=twilio_from, twilio_messaging_service_sid=twilio_messaging_service_sid,
            sender_number=sender_number, self_number=self_number,
        )
    except sms_link.SmsLinkError as e:
        # Re-render with what they typed, minus the secrets, which are never
        # sent back to the browser even in an error round-trip.
        return _page(request, db, user, error=str(e), form={
            "provider": provider or "gateway",
            "gateway_url": gateway_url, "gateway_user": gateway_user,
            "gateway_path_relay": bool(gateway_path_relay),
            "gateway_device_id": gateway_device_id,
            "gateway_encrypt": bool(gateway_encrypt),
            "twilio_account_sid": twilio_account_sid, "twilio_from": twilio_from,
            "twilio_messaging_service_sid": twilio_messaging_service_sid,
            "sender_number": sender_number, "self_number": self_number,
        })
    return RedirectResponse("/account/sms?notice=saved", status_code=303)


@router.post("/account/sms/test", response_class=HTMLResponse)
# A real text each time, so this is a guard against a stuck finger, not an
# attacker — sign-in is invite-only.
@limiter.limit("5/minute")
def test(request: Request, db: Session = Depends(get_db)):
    user, redirect = _gate(request, db)
    if redirect is not None:
        return redirect
    link = sms_link.get(db, user)
    if link is None:
        return RedirectResponse("/account/sms", status_code=303)
    error = sms_link.test_send(db, link, get_settings())
    if error:
        return _page(request, db, user, error=error)
    return RedirectResponse("/account/sms?notice=tested", status_code=303)


@router.post("/account/sms/webhooks", response_class=HTMLResponse)
@limiter.limit("10/minute")
def register_webhooks(request: Request, db: Session = Depends(get_db)):
    user, redirect = _gate(request, db)
    if redirect is not None:
        return redirect
    link = sms_link.get(db, user)
    if link is None:
        return RedirectResponse("/account/sms", status_code=303)
    error = sms_link.register_gateway_webhooks(db, link, get_settings())
    if error:
        return _page(request, db, user, error=error)
    return RedirectResponse("/account/sms?notice=webhooks", status_code=303)


@router.post("/account/sms/remove")
def remove(request: Request, db: Session = Depends(get_db)):
    user, redirect = _gate(request, db)
    if redirect is not None:
        return redirect
    sms_link.remove(db, user)
    return RedirectResponse("/account/sms?notice=removed", status_code=303)
