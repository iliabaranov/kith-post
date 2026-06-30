"""Public recipient pages, reached by opaque token from the emailed link:
the invitation, its image, and its .ics. (RSVP persistence + open tracking
land in G4 — for now the page renders and the RSVP works client-side.)"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from kith.config import get_settings
from kith.core import calendar as cal
from kith.db.models import Asset, Event, Recipient, User
from kith.web.deps import get_db, templates

router = APIRouter()


def _recipient(db: Session, token: str) -> Recipient | None:
    return db.execute(select(Recipient).where(Recipient.token == token)).scalar_one_or_none()


@router.get("/i/{token}", response_class=HTMLResponse)
def view_invite(token: str, request: Request, db: Session = Depends(get_db)):
    r = _recipient(db, token)
    ev = db.get(Event, r.event_id) if r else None
    if ev is None:
        return templates.TemplateResponse(
            request, "invite_404.html", {"settings": get_settings()}, status_code=404
        )
    owner = db.get(User, ev.user_id)
    asset = db.get(Asset, ev.asset_id) if ev.asset_id else None
    gcal_url = cal.build_google_url(cal.from_event(ev))
    ctx = {
        "settings": get_settings(),
        "event": ev,
        "blocks": ev.blocks or {},
        "host_name": owner.display_name if owner else "",
        "image_url": (f"/i/{token}/image" if asset else None),
        "gcal_url": gcal_url,
        "ics_url": (f"/i/{token}/calendar.ics" if gcal_url else None),
        "preview": False,
    }
    return templates.TemplateResponse(request, "invite.html", ctx)


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
    body = cal.build_ics(cal.from_event(ev), dtstamp=datetime.now(UTC))
    if body is None:
        return Response(status_code=404)
    return Response(
        body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="kith-post.ics"'},
    )
