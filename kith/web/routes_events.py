"""Compose-a-card routes (G2): create/edit an event, preview it, serve its image."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kith.config import SendMode, get_settings
from kith.core import calendar as cal
from kith.core import images
from kith.core import recipients as rcpt
from kith.core.tracking import new_token
from kith.db.models import Asset, Event, Recipient, Reminder
from kith.services import contacts as book
from kith.services import scheduler, send, storage
from kith.web.deps import get_db, load_user, templates

router = APIRouter()

# Full-invite default. A plain holiday card = turn the blocks off.
# A new card starts blank — every optional block off. The host ticks what they
# want (each tick reveals its field), so a card is an invitation or a plain
# holiday card depending only on what they choose to add.
DEFAULT_BLOCKS = {
    "message": False, "date": False, "time": False,
    "location": False, "rsvp": False, "headcount": False, "allergies": False,
}


def _parse_date(s: str) -> date | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _parse_int(s: str, lo: int = 1, hi: int = 30) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return max(lo, min(int(s), hi))
    except ValueError:
        return None


def _blocks_from_form(message, date_, time_, location_, rsvp, headcount, allergies) -> dict:
    """What shows on the card. Each block is on when its checkbox is ticked
    (arrives "on" / None). The form reveals each field only while its box is
    checked, so you can't fill a hidden field and have it silently vanish. Time
    only counts alongside a date — a bare time is meaningless on an invitation."""
    return {
        "message": message is not None,
        "date": date_ is not None,
        "time": time_ is not None and date_ is not None,
        "location": location_ is not None,
        "rsvp": rsvp is not None,
        "headcount": headcount is not None,
        "allergies": allergies is not None,
    }


def _owned_event(db: Session, user_id: str, event_id: str) -> Event | None:
    ev = db.get(Event, event_id)
    return ev if ev and ev.user_id == user_id else None


def _recipient_count(db: Session, event_id: str) -> int:
    return db.execute(
        select(func.count()).select_from(Recipient).where(Recipient.event_id == event_id)
    ).scalar_one()


def _queued_count(db: Session, event_id: str) -> int:
    return db.execute(
        select(func.count()).select_from(Recipient)
        .where(Recipient.event_id == event_id, Recipient.status == "queued")
    ).scalar_one()


_STATE_LABEL = {
    "coming": "Coming", "declined": "Can't make it",
    "opened": "Opened", "sent": "Sent", "queued": "Not sent",
}


def _recipient_state(r: Recipient) -> str:
    """The one status to show the host, newest signal wins."""
    if r.status in ("coming", "declined"):
        return r.status
    if r.first_open_at:
        return "opened"
    if r.sent_at:
        return "sent"
    return "queued"


def _rsvp_summary(rows: list[Recipient]) -> tuple[dict, list[dict]]:
    coming = [r for r in rows if r.status == "coming"]
    declined = [r for r in rows if r.status == "declined"]
    stats = {
        "total": len(rows),
        "opened": sum(1 for r in rows if r.first_open_at),
        "coming": len(coming),
        "guests": sum((r.party_size or 1) for r in coming),
        "declined": len(declined),
        "no_reply": len(rows) - len(coming) - len(declined),
    }
    recipients = []
    for r in rows:
        state = _recipient_state(r)
        when = ""
        if state in ("coming", "declined") and r.rsvp_at:
            when = r.rsvp_at.strftime("%b %d")
        elif state == "opened" and r.first_open_at:
            when = r.first_open_at.strftime("%b %d")
        recipients.append({
            "name": r.name or r.email, "email": r.email,
            "state": state, "label": _STATE_LABEL[state],
            "party_size": r.party_size, "when": when,
            "adults": r.adults, "kids": r.kids,
            "note": r.note, "allergies": r.allergies,
        })
    return stats, recipients


_SEND_UI = {
    "dry-run": ("Send (dry run)", "Writes .eml files to data/outbox — no real email is sent.",
                "Write {n} dry-run email(s) to the outbox?"),
    "self-only": ("Send a test to yourself", "Sends only to your own inbox, for testing.",
                  "Send {n} test email(s) to your own inbox?"),
    "live": ("Send to {n} now", "Sends from your Gmail to everyone still queued.",
             "Send {n} real {noun}(s) from your Gmail now?"),
}


def _event_noun(ev) -> str:  # noqa: ANN001 — a DB Event
    """Host-facing noun. Something that asks for a reply or is pinned to a date is
    an 'invitation'; a plain image/title/message greeting is a 'card'."""
    blocks = ev.blocks or {}
    is_invite = bool(blocks.get("rsvp") or (blocks.get("date") and ev.event_date))
    return "invitation" if is_invite else "card"


@dataclass
class ReconcileResult:
    added: int
    removed: int
    kept: int
    invalid: list[str]


def _reconcile_recipients(db: Session, event_id: str, text: str) -> ReconcileResult:
    """Update an event's recipient list without discarding existing rows.

    Match by normalized email: add new addresses (fresh token, queued), keep
    matched rows untouched (only fill an empty name), remove absent ones. Keeping
    rows preserves each recipient's token, sent/RSVP state, and mail threading —
    unlike a delete-and-recreate, which wiped all of that on every edit.
    """
    valid, invalid = rcpt.parse_recipients(text)
    existing = db.execute(
        select(Recipient).where(Recipient.event_id == event_id)
    ).scalars().all()
    by_email: dict[str, Recipient] = {}
    stale: list[Recipient] = []  # legacy duplicate rows for the same email
    for r in existing:
        key = rcpt.normalize(r.email)
        (stale.append(r) if key in by_email else by_email.setdefault(key, r))
    wanted = {p.email: p for p in valid}  # p.email is already normalized

    added = kept = removed = 0
    for email, p in wanted.items():
        row = by_email.get(email)
        if row is None:
            db.add(Recipient(event_id=event_id, email=p.email, name=p.name, token=new_token()))
            added += 1
        else:
            if p.name and not row.name:  # fill a missing name, never overwrite
                row.name = p.name
            kept += 1
    for email, row in by_email.items():
        if email not in wanted:
            db.delete(row)  # FK cascade drops any reminders for this recipient
            removed += 1
    for row in stale:
        db.delete(row)
        removed += 1
    db.commit()
    return ReconcileResult(added=added, removed=removed, kept=kept, invalid=invalid)


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
        "contacts": book.list_contacts(db, user.id),
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
    event_end_time: str = Form(""),
    location: str = Form(""),
    signoff: str = Form(""),
    headcount_max: str = Form(""),
    timezone: str = Form(""),
    recipients: str = Form(""),
    block_message: str | None = Form(None),
    block_date: str | None = Form(None),
    block_time: str | None = Form(None),
    block_location: str | None = Form(None),
    block_rsvp: str | None = Form(None),
    block_headcount: str | None = Form(None),
    block_allergies: str | None = Form(None),
    image: UploadFile | None = File(None),
):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    blocks = _blocks_from_form(
        block_message, block_date, block_time, block_location, block_rsvp,
        block_headcount, block_allergies
    )
    try:
        derived = await _read_image(image)
    except images.ImageError as e:
        ctx = {
            "settings": get_settings(), "user": user, "event": None, "blocks": blocks,
            "recipients_text": recipients, "error": str(e),
            "contacts": book.list_contacts(db, user.id),
        }
        return templates.TemplateResponse(request, "event_form.html", ctx, status_code=400)

    asset = storage.store_asset(db, user.id, derived) if derived else None
    ev = Event(
        user_id=user.id,
        title=title.strip(),
        message=message.strip(),
        event_date=_parse_date(event_date),
        event_time=(event_time.strip() or None),
        event_end_time=(event_end_time.strip() or None),
        location=(location.strip() or None),
        signoff=(signoff.strip() or None),
        blocks=blocks,
        headcount_max=_parse_int(headcount_max),
        timezone=(timezone.strip() or None),
        asset_id=asset.id if asset else None,
        status="draft",
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    _reconcile_recipients(db, ev.id, recipients)
    return RedirectResponse(f"/events/{ev.id}?ask_contacts=1", status_code=303)


_OFFSET_PHRASE = {
    "halfway": "about halfway to the date",
    "7d": "1 week before",
    "3d": "3 days before",
}


def _fmt_hour(h: int) -> str:
    return f"{h % 12 or 12} {'am' if h < 12 else 'pm'}"


def _describe_schedule(cfg) -> str:
    parts = [_OFFSET_PHRASE.get(o, o) for o in cfg.offsets]
    if len(parts) > 1:
        joined = ", ".join(parts[:-1]) + ", and " + parts[-1]
    else:
        joined = parts[0] if parts else ""
    return f"{joined}, around {_fmt_hour(cfg.send_hour_local)} in the event's timezone"


def _reminders_ui(db: Session, ev: Event, settings, recipients: list[Recipient]) -> dict:
    """Host-facing reminder summary for the event page."""
    cfg = scheduler.resolved_cfg(settings, ev)
    rows = db.execute(select(Reminder).where(Reminder.event_id == ev.id)).scalars().all()
    pending = sorted(rows, key=lambda r: scheduler._as_utc(r.scheduled_for))
    pending = [r for r in pending if r.status == "pending"]
    tz = None
    if ev.timezone:
        try:
            tz = ZoneInfo(ev.timezone)
        except Exception:
            tz = None

    def _fmt(dt) -> str:
        d = scheduler._as_utc(dt)
        if tz is not None:
            d = d.astimezone(tz)
        return d.strftime("%a, %b %d at %I:%M %p").replace(" 0", " ")

    return {
        "available": bool(ev.event_date) and bool((ev.blocks or {}).get("rsvp")),
        "enabled": cfg.enabled,
        # "sent" here means the event has actually gone out (so we don't tell the
        # host reminders will schedule "once you send" after they already have).
        "sent_any": any(r.status in ("sent", "coming", "declined") for r in recipients),
        "scheduled": len(pending),
        "sent": sum(1 for r in rows if r.status == "sent"),
        "planned": [_fmt(r.scheduled_for) for r in pending],
        "schedule_desc": _describe_schedule(cfg),
        "max_per_recipient": cfg.max_per_recipient,
    }


@router.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(
    event_id: str, request: Request, db: Session = Depends(get_db),
    sent: int = 0, failed: int = 0, ask_contacts: int = 0, saved: int = 0,
    details_changed: int = 0,
):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ev = _owned_event(db, user.id, event_id)
    if ev is None:
        return RedirectResponse("/", status_code=303)
    settings = get_settings()
    rows = db.execute(
        select(Recipient).where(Recipient.event_id == ev.id).order_by(Recipient.created_at)
    ).scalars().all()
    queued = sum(1 for r in rows if r.status == "queued")
    label, hint, confirm = _SEND_UI.get(settings.send_mode.value, _SEND_UI["dry-run"])
    noun = _event_noun(ev)
    # right after create/edit, offer to save recipients who aren't in the book yet
    new_contacts = 0
    if ask_contacts:
        parsed = [rcpt.Parsed(name=r.name, email=r.email) for r in rows]
        new_contacts = len(book.new_among(db, user.id, parsed))
    stats, recipients = _rsvp_summary(rows)
    resendable = sum(1 for r in rows if r.status in ("sent", "coming", "declined"))
    ctx = {
        "settings": settings, "user": user, "event": ev,
        "recipient_count": len(rows), "queued_count": queued,
        "send_mode": settings.send_mode.value,
        "send_label": label.format(n=queued), "send_hint": hint,
        "send_confirm": confirm.format(n=queued, noun=noun), "noun": noun,
        "sent": sent, "failed": failed,
        "new_contacts": new_contacts, "saved": saved,
        "stats": stats, "recipients": recipients,
        "reminders": _reminders_ui(db, ev, settings, rows),
        "details_changed": bool(details_changed), "resendable": resendable,
    }
    return templates.TemplateResponse(request, "event_detail.html", ctx)


@router.post("/events/{event_id}/send")
def send_invitations(event_id: str, request: Request, db: Session = Depends(get_db)):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ev = _owned_event(db, user.id, event_id)
    if ev is None:
        return RedirectResponse("/", status_code=303)
    settings = get_settings()
    if settings.send_mode != SendMode.dry_run and not user.refresh_token:
        return RedirectResponse(f"/events/{ev.id}?failed=1", status_code=303)
    result = send.send_event(db, ev, user, settings)
    scheduler.schedule_event_reminders(db, ev, settings)  # nudge non-responders (G5)
    return RedirectResponse(
        f"/events/{ev.id}?sent={result.sent}&failed={result.failed}", status_code=303
    )


@router.post("/events/{event_id}/reminders")
def toggle_reminders(
    event_id: str, request: Request, db: Session = Depends(get_db),
    enabled: str | None = Form(None),
):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ev = _owned_event(db, user.id, event_id)
    if ev is None:
        return RedirectResponse("/", status_code=303)
    on = enabled is not None
    ev.reminder_cfg = {**(ev.reminder_cfg or {}), "enabled": on}
    db.commit()
    settings = get_settings()
    if on:
        scheduler.schedule_event_reminders(db, ev, settings)  # (re)build for sent recipients
    else:
        scheduler.cancel_all_pending_for_event(db, ev.id, reason="disabled")
    return RedirectResponse(f"/events/{ev.id}", status_code=303)


@router.post("/events/{event_id}/resend")
def resend_updated(event_id: str, request: Request, db: Session = Depends(get_db)):
    """Details changed → re-send the updated card to everyone already invited and
    re-collect their RSVPs. Resets them to unanswered (keeping token + thread so the
    re-send threads under the original), re-sends with a note, reschedules reminders."""
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ev = _owned_event(db, user.id, event_id)
    if ev is None:
        return RedirectResponse("/", status_code=303)
    settings = get_settings()
    if settings.send_mode != SendMode.dry_run and not user.refresh_token:
        return RedirectResponse(f"/events/{ev.id}?failed=1", status_code=303)
    targets = db.execute(
        select(Recipient).where(
            Recipient.event_id == ev.id, Recipient.status.in_(("sent", "coming", "declined"))
        )
    ).scalars().all()
    for r in targets:  # re-collect: clear prior response, keep token + thread_id
        r.status = "queued"
        r.sent_at = None
        r.first_open_at = None
        r.rsvp_at = None
        r.party_size = None
        r.adults = None
        r.kids = None
        r.note = None
        r.allergies = None
    scheduler.cancel_all_pending_for_event(db, ev.id, reason="resend")
    db.commit()
    result = send.send_event(
        db, ev, user, settings, note="Some details have changed — here's the latest."
    )
    scheduler.schedule_event_reminders(db, ev, settings)
    return RedirectResponse(
        f"/events/{ev.id}?sent={result.sent}&failed={result.failed}", status_code=303
    )


@router.post("/events/{event_id}/delete")
def delete_event(event_id: str, request: Request, db: Session = Depends(get_db)):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ev = _owned_event(db, user.id, event_id)
    if ev is None:
        return RedirectResponse("/", status_code=303)
    asset = db.get(Asset, ev.asset_id) if ev.asset_id else None
    db.query(Recipient).filter(Recipient.event_id == ev.id).delete()
    db.delete(ev)
    db.commit()
    if asset is not None:
        storage.delete_asset(db, asset)
    return RedirectResponse("/", status_code=303)


@router.post("/events/{event_id}/save-contacts")
def save_contacts(event_id: str, request: Request, db: Session = Depends(get_db)):
    """Add this event's recipients to the address book (dedup is automatic)."""
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ev = _owned_event(db, user.id, event_id)
    if ev is None:
        return RedirectResponse("/", status_code=303)
    rows = db.execute(select(Recipient).where(Recipient.event_id == ev.id)).scalars().all()
    added = sum(book.add_contact(db, user.id, r.email, r.name)[1] for r in rows)
    return RedirectResponse(f"/events/{ev.id}?saved={added}", status_code=303)


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
        "contacts": book.list_contacts(db, user.id),
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
    event_end_time: str = Form(""),
    location: str = Form(""),
    signoff: str = Form(""),
    headcount_max: str = Form(""),
    timezone: str = Form(""),
    recipients: str = Form(""),
    block_message: str | None = Form(None),
    block_date: str | None = Form(None),
    block_time: str | None = Form(None),
    block_location: str | None = Form(None),
    block_rsvp: str | None = Form(None),
    block_headcount: str | None = Form(None),
    block_allergies: str | None = Form(None),
    image: UploadFile | None = File(None),
):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ev = _owned_event(db, user.id, event_id)
    if ev is None:
        return RedirectResponse("/", status_code=303)
    blocks = _blocks_from_form(
        block_message, block_date, block_time, block_location, block_rsvp,
        block_headcount, block_allergies
    )
    try:
        derived = await _read_image(image)
    except images.ImageError as e:
        ctx = {
            "settings": get_settings(), "user": user, "event": ev, "blocks": blocks,
            "recipients_text": recipients, "error": str(e),
            "contacts": book.list_contacts(db, user.id),
        }
        return templates.TemplateResponse(request, "event_form.html", ctx, status_code=400)

    # Snapshot the details that affect whether someone can attend, to detect a
    # change worth re-sending for (date / time / location).
    before = (ev.event_date, ev.event_time, ev.event_end_time, ev.location)
    ev.title = title.strip()
    ev.message = message.strip()
    ev.event_date = _parse_date(event_date)
    ev.event_time = (event_time.strip() or None)
    ev.event_end_time = (event_end_time.strip() or None)
    ev.location = (location.strip() or None)
    ev.signoff = (signoff.strip() or None)
    ev.headcount_max = _parse_int(headcount_max)
    ev.timezone = (timezone.strip() or None)
    ev.blocks = blocks
    if derived is not None:
        ev.asset_id = storage.store_asset(db, user.id, derived).id
    details_changed = before != (ev.event_date, ev.event_time, ev.event_end_time, ev.location)
    db.commit()
    _reconcile_recipients(db, ev.id, recipients)
    already_sent = db.execute(
        select(func.count()).select_from(Recipient).where(
            Recipient.event_id == ev.id, Recipient.status.in_(("sent", "coming", "declined"))
        )
    ).scalar_one()
    q = "?ask_contacts=1"
    if details_changed and already_sent:
        q += "&details_changed=1"
    return RedirectResponse(f"/events/{ev.id}{q}", status_code=303)


@router.get("/events/{event_id}/preview", response_class=HTMLResponse)
def preview_event(event_id: str, request: Request, db: Session = Depends(get_db)):
    """The full interactive invite, exactly as a recipient sees it (owner preview)."""
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ev = _owned_event(db, user.id, event_id)
    if ev is None:
        return RedirectResponse("/", status_code=303)
    asset = db.get(Asset, ev.asset_id) if ev.asset_id else None
    gcal_url = cal.build_google_url(cal.from_event(ev))
    ctx = {
        "settings": get_settings(),
        "event": ev,
        "blocks": ev.blocks or {},
        "host_name": user.display_name,
        "image_url": (f"/assets/{asset.id}" if asset else None),
        "gcal_url": gcal_url,
        "ics_url": (f"/events/{ev.id}/calendar.ics" if gcal_url else None),
        "preview": True,
        "token": None, "rsvp_status": None, "party_size": None,
        "locked": False, "editing": False,
    }
    return templates.TemplateResponse(request, "invite.html", ctx)


@router.get("/events/{event_id}/calendar.ics")
def event_ics(event_id: str, request: Request, db: Session = Depends(get_db)):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ev = _owned_event(db, user.id, event_id)
    if ev is None:
        return RedirectResponse("/", status_code=303)
    body = cal.build_ics(cal.from_event(ev), dtstamp=datetime.now(UTC))
    if body is None:
        return RedirectResponse(f"/events/{ev.id}", status_code=303)
    return Response(
        body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="kith-post.ics"'},
    )


@router.get("/assets/{asset_id}")
def serve_asset(asset_id: str, request: Request, db: Session = Depends(get_db)):
    user = load_user(request, db)
    asset = db.get(Asset, asset_id)
    if user is None or asset is None or asset.user_id != user.id:
        return RedirectResponse("/", status_code=303)
    # fall back to the inline copy if the full-res file was auto-purged
    path = asset.full_path if Path(asset.full_path).exists() else asset.inline_path
    return FileResponse(path, media_type=asset.mime)
