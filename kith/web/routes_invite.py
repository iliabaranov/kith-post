"""Public recipient pages, reached by opaque token from the emailed link:
the invitation, its image, its .ics, and the RSVP itself (G4).

"Opened" = the first landing visit (no tracking pixel). RSVP persistence keeps
only the latest state on the Recipient row (status + party_size + timestamps).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from kith.config import get_settings
from kith.core import calendar as cal
from kith.db.models import Asset, Event, Recipient, User
from kith.services import scheduler
from kith.web.deps import get_db, templates

log = logging.getLogger("kith")
router = APIRouter()


def _recipient(db: Session, token: str) -> Recipient | None:
    return db.execute(select(Recipient).where(Recipient.token == token)).scalar_one_or_none()


def _is_locked(ev: Event) -> bool:
    """Replies close once the event date has passed (per the locked design)."""
    return bool(ev.event_date and ev.event_date < date.today())


@router.get("/i/{token}", response_class=HTMLResponse)
def view_invite(
    token: str, request: Request, db: Session = Depends(get_db), edit: int = 0
):
    r = _recipient(db, token)
    ev = db.get(Event, r.event_id) if r else None
    if r is None or ev is None:
        return templates.TemplateResponse(
            request, "invite_404.html", {"settings": get_settings()}, status_code=404
        )
    if r.first_open_at is None:  # "Opened" = first landing visit, no pixel
        r.first_open_at = datetime.now(UTC)
        db.commit()
    owner = db.get(User, ev.user_id)
    asset = db.get(Asset, ev.asset_id) if ev.asset_id else None
    rsvp_url = f"{get_settings().base_url.rstrip('/')}/i/{token}"
    gcal_url = cal.build_google_url(cal.from_event(ev, rsvp_url=rsvp_url))
    locked = _is_locked(ev)
    ctx = {
        "settings": get_settings(),
        "event": ev,
        "blocks": ev.blocks or {},
        "host_name": owner.display_name if owner else "",
        "image_url": (f"/i/{token}/image" if asset else None),
        "gcal_url": gcal_url,
        "ics_url": (f"/i/{token}/calendar.ics" if gcal_url else None),
        "preview": False,
        "token": token,
        "rsvp_status": r.status if r.status in ("coming", "declined") else None,
        "party_size": r.party_size,
        "note": r.note,
        "allergies": r.allergies,
        "locked": locked,
        "editing": bool(edit) and not locked,
    }
    return templates.TemplateResponse(request, "invite.html", ctx)


@router.post("/i/{token}/rsvp")
def submit_rsvp(
    token: str, request: Request, db: Session = Depends(get_db),
    response: str = Form(""), party_size: str = Form(""),
    note: str = Form(""), allergies: str = Form(""),
):
    r = _recipient(db, token)
    ev = db.get(Event, r.event_id) if r else None
    if r is None or ev is None:
        return Response(status_code=404)
    if _is_locked(ev):  # replies closed — ignore, just show the page
        return RedirectResponse(f"/i/{token}", status_code=303)
    if response == "coming":
        cap = ev.headcount_max or 30
        try:
            n = int(party_size)
        except (TypeError, ValueError):
            n = 1
        r.party_size = max(1, min(n, cap))  # never trust the client stepper
        r.status = "coming"
    elif response == "declined":
        r.party_size = None
        r.status = "declined"
    else:
        return RedirectResponse(f"/i/{token}", status_code=303)
    r.note = note.strip() or None
    r.allergies = (allergies.strip() or None) if (ev.blocks or {}).get("allergies") else None
    r.rsvp_at = datetime.now(UTC)
    if r.first_open_at is None:
        r.first_open_at = r.rsvp_at
    db.commit()
    # They responded — stop any future nudges. Never let a scheduler hiccup break
    # the recipient's page.
    try:
        scheduler.cancel_pending_reminders(db, r.id)
    except Exception:
        log.exception("failed to cancel reminders after RSVP (recipient %s)", r.id)
    return RedirectResponse(f"/i/{token}", status_code=303)


@router.get("/i/{token}/image")
def invite_image(token: str, request: Request, db: Session = Depends(get_db)):
    r = _recipient(db, token)
    ev = db.get(Event, r.event_id) if r else None
    asset = db.get(Asset, ev.asset_id) if ev and ev.asset_id else None
    if asset is None:
        return Response(status_code=404)
    return FileResponse(asset.full_path, media_type=asset.mime)


@router.get("/i/{token}/calendar.ics")
def invite_ics(token: str, request: Request, db: Session = Depends(get_db)):
    r = _recipient(db, token)
    ev = db.get(Event, r.event_id) if r else None
    if ev is None:
        return Response(status_code=404)
    rsvp_url = f"{get_settings().base_url.rstrip('/')}/i/{token}"
    body = cal.build_ics(cal.from_event(ev, rsvp_url=rsvp_url), dtstamp=datetime.now(UTC))
    if body is None:
        return Response(status_code=404)
    return Response(
        body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="kith-post.ics"'},
    )
