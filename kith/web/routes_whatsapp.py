"""Link a host's own WhatsApp account (the WhatsApp delivery channel).

The flow is deliberately gated: nothing is created in WAHA until the host has
acknowledged that this uses an unofficial client and their WhatsApp account could
be restricted or banned. That warning is a product requirement, not a footnote —
we're asking someone to point a third-party client at their personal account.

Pairing state is polled rather than pushed: ``/account/whatsapp/state`` returns
JSON for the page's small poller, and every page also works without JavaScript
via a plain "check again" button.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from kith.config import get_settings
from kith.services import wa_session as link
from kith.services import waha
from kith.web.deps import get_db, load_user, templates

router = APIRouter()

# What the host is asked to do next, per WAHA session status. Copy lives here so
# the template stays about layout.
_PROMPTS = {
    waha.STATUS_SCAN_QR: "Scan this code with WhatsApp on your phone.",
    waha.STATUS_PASSKEY: "WhatsApp is asking for a passkey — confirm it on your phone.",
    waha.STATUS_PASSKEY_CONFIRM: "Confirm the pairing on your phone to finish.",
    waha.STATUS_STARTING: "Waking up your WhatsApp connection…",
    waha.STATUS_STOPPED: "Your connection is stopped.",
    waha.STATUS_FAILED: "That pairing attempt didn't finish. Try linking again.",
}


def _page(request: Request, db: Session, user, *, error: str | None = None):  # noqa: ANN001
    settings = get_settings()
    state = link.refresh(db, user, settings)
    status = state.status if state else None
    return templates.TemplateResponse(
        request,
        "whatsapp.html",
        {
            "settings": settings,
            "user": user,
            "state": state,
            "status": status,
            "prompt": _PROMPTS.get(status or ""),
            "acknowledged": link.acknowledged(user),
            "linked": bool(state and state.is_working),
            "pairing": bool(state and state.is_pairing),
            "error": error,
        },
    )


@router.get("/account/whatsapp", response_class=HTMLResponse)
def whatsapp_page(request: Request, db: Session = Depends(get_db)):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    if not link.available(get_settings()):
        return RedirectResponse("/account", status_code=303)
    return _page(request, db, user)


@router.post("/account/whatsapp/acknowledge")
def acknowledge(request: Request, db: Session = Depends(get_db)):
    """The host has read the warning. Only after this can a session exist."""
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    if not link.available(get_settings()):
        return RedirectResponse("/account", status_code=303)
    link.acknowledge(db, user)
    return RedirectResponse("/account/whatsapp", status_code=303)


@router.post("/account/whatsapp/link")
def start_link(request: Request, db: Session = Depends(get_db)):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    settings = get_settings()
    if not link.available(settings):
        return RedirectResponse("/account", status_code=303)
    try:
        link.start_link(db, user, settings)
    except waha.WahaError as e:
        # Includes "WAHA is unreachable", which is an operator problem, not the
        # host's — say so plainly rather than showing a broken QR frame.
        return _page(request, db, user, error=str(e))
    return RedirectResponse("/account/whatsapp", status_code=303)


@router.post("/account/whatsapp/unlink")
def unlink(request: Request, db: Session = Depends(get_db)):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    link.unlink(db, user, get_settings())
    return RedirectResponse("/account/whatsapp?unlinked=1", status_code=303)


@router.get("/account/whatsapp/qr.png")
def qr(request: Request, db: Session = Depends(get_db)):
    """Proxy the pairing QR from WAHA.

    Served by us so the WAHA API key never has to reach the browser. Explicitly
    uncacheable: a stale QR is an unscannable QR.
    """
    user = load_user(request, db)
    if user is None:
        return Response(status_code=404)
    try:
        png = link.qr_png(user, get_settings())
    except waha.WahaError:
        return Response(status_code=404)
    return Response(
        png,
        media_type="image/png",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/account/whatsapp/state")
def state(request: Request, db: Session = Depends(get_db)):
    """Current pairing state as JSON, for the page's poller."""
    user = load_user(request, db)
    if user is None:
        return {"status": None, "linked": False}
    st = link.refresh(db, user, get_settings())
    return {
        "status": st.status if st else None,
        "linked": bool(st and st.is_working),
        "pairing": bool(st and st.is_pairing),
        "number": st.phone if st else None,
        "prompt": _PROMPTS.get(st.status if st else ""),
    }
