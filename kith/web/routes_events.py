"""Compose-a-card routes (G2): create/edit an event, preview it, serve its image."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kith.config import get_settings
from kith.core import images
from kith.core import recipients as rcpt
from kith.core.tracking import new_token
from kith.db.models import Asset, Event, Recipient
from kith.services import storage
from kith.web.deps import get_db, load_user, templates

router = APIRouter()

# Full-invite default. A plain holiday card = turn the blocks off.
DEFAULT_BLOCKS = {
    "message": True, "date": True, "time": True,
    "location": True, "rsvp": True, "headcount": False,
}


def _parse_date(s: str) -> date | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _blocks_from_form(message, date_, time_, location_, rsvp, headcount) -> dict:
    # checkboxes arrive as "on" when ticked, None when not
    return {
        "message": message is not None,
        "date": date_ is not None,
        "time": time_ is not None,
        "location": location_ is not None,
        "rsvp": rsvp is not None,
        "headcount": headcount is not None,
    }


def _owned_event(db: Session, user_id: str, event_id: str) -> Event | None:
    ev = db.get(Event, event_id)
    return ev if ev and ev.user_id == user_id else None


def _recipient_count(db: Session, event_id: str) -> int:
    return db.execute(
        select(func.count()).select_from(Recipient).where(Recipient.event_id == event_id)
    ).scalar_one()


def _replace_recipients(db: Session, event_id: str, text: str) -> tuple[int, list[str]]:
    valid, invalid = rcpt.parse_recipients(text)
    db.query(Recipient).filter(Recipient.event_id == event_id).delete()
    for p in valid:
        db.add(Recipient(event_id=event_id, email=p.email, name=p.name, token=new_token()))
    db.commit()
    return len(valid), invalid


async def _read_image(image: UploadFile | None) -> images.Derived | None:
    if image is None or not image.filename:
        return None
    return images.process(await image.read())


@router.get("/events/new", response_class=HTMLResponse)
def new_event(request: Request, db: Session = Depends(get_db)):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ctx = {
        "settings": get_settings(), "user": user, "event": None,
        "blocks": DEFAULT_BLOCKS, "recipients_text": "", "error": None,
    }
    return templates.TemplateResponse(request, "event_form.html", ctx)


@router.post("/events")
async def create_event(
    request: Request,
    db: Session = Depends(get_db),
    title: str = Form(""),
    message: str = Form(""),
    event_date: str = Form(""),
    event_time: str = Form(""),
    location: str = Form(""),
    recipients: str = Form(""),
    block_message: str | None = Form(None),
    block_date: str | None = Form(None),
    block_time: str | None = Form(None),
    block_location: str | None = Form(None),
    block_rsvp: str | None = Form(None),
    block_headcount: str | None = Form(None),
    image: UploadFile | None = File(None),
):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    blocks = _blocks_from_form(
        block_message, block_date, block_time, block_location, block_rsvp, block_headcount
    )
    try:
        derived = await _read_image(image)
    except images.ImageError as e:
        ctx = {
            "settings": get_settings(), "user": user, "event": None, "blocks": blocks,
            "recipients_text": recipients, "error": str(e),
        }
        return templates.TemplateResponse(request, "event_form.html", ctx, status_code=400)

    asset = storage.store_asset(db, user.id, derived) if derived else None
    ev = Event(
        user_id=user.id,
        title=title.strip(),
        message=message.strip(),
        event_date=_parse_date(event_date),
        event_time=(event_time.strip() or None),
        location=(location.strip() or None),
        blocks=blocks,
        asset_id=asset.id if asset else None,
        status="draft",
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    _replace_recipients(db, ev.id, recipients)
    return RedirectResponse(f"/events/{ev.id}", status_code=303)


@router.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(event_id: str, request: Request, db: Session = Depends(get_db)):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ev = _owned_event(db, user.id, event_id)
    if ev is None:
        return RedirectResponse("/", status_code=303)
    asset = db.get(Asset, ev.asset_id) if ev.asset_id else None
    ctx = {
        "settings": get_settings(), "user": user, "event": ev, "asset": asset,
        "recipient_count": _recipient_count(db, ev.id), "preview": True,
    }
    return templates.TemplateResponse(request, "event_detail.html", ctx)


@router.get("/events/{event_id}/edit", response_class=HTMLResponse)
def edit_event(event_id: str, request: Request, db: Session = Depends(get_db)):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ev = _owned_event(db, user.id, event_id)
    if ev is None:
        return RedirectResponse("/", status_code=303)
    rows = db.execute(select(Recipient).where(Recipient.event_id == ev.id)).scalars().all()
    recipients_text = "\n".join(
        (f"{r.name} <{r.email}>" if r.name else r.email) for r in rows
    )
    ctx = {
        "settings": get_settings(), "user": user, "event": ev,
        "blocks": ev.blocks or DEFAULT_BLOCKS, "recipients_text": recipients_text, "error": None,
    }
    return templates.TemplateResponse(request, "event_form.html", ctx)


@router.post("/events/{event_id}")
async def update_event(
    event_id: str,
    request: Request,
    db: Session = Depends(get_db),
    title: str = Form(""),
    message: str = Form(""),
    event_date: str = Form(""),
    event_time: str = Form(""),
    location: str = Form(""),
    recipients: str = Form(""),
    block_message: str | None = Form(None),
    block_date: str | None = Form(None),
    block_time: str | None = Form(None),
    block_location: str | None = Form(None),
    block_rsvp: str | None = Form(None),
    block_headcount: str | None = Form(None),
    image: UploadFile | None = File(None),
):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ev = _owned_event(db, user.id, event_id)
    if ev is None:
        return RedirectResponse("/", status_code=303)
    try:
        derived = await _read_image(image)
    except images.ImageError as e:
        ctx = {
            "settings": get_settings(), "user": user, "event": ev,
            "blocks": _blocks_from_form(block_message, block_date, block_time,
                                        block_location, block_rsvp, block_headcount),
            "recipients_text": recipients, "error": str(e),
        }
        return templates.TemplateResponse(request, "event_form.html", ctx, status_code=400)

    ev.title = title.strip()
    ev.message = message.strip()
    ev.event_date = _parse_date(event_date)
    ev.event_time = (event_time.strip() or None)
    ev.location = (location.strip() or None)
    ev.blocks = _blocks_from_form(
        block_message, block_date, block_time, block_location, block_rsvp, block_headcount
    )
    if derived is not None:
        ev.asset_id = storage.store_asset(db, user.id, derived).id
    db.commit()
    _replace_recipients(db, ev.id, recipients)
    return RedirectResponse(f"/events/{ev.id}", status_code=303)


@router.get("/assets/{asset_id}")
def serve_asset(asset_id: str, request: Request, db: Session = Depends(get_db)):
    user = load_user(request, db)
    asset = db.get(Asset, asset_id)
    if user is None or asset is None or asset.user_id != user.id:
        return RedirectResponse("/", status_code=303)
    return FileResponse(asset.full_path, media_type=asset.mime)
