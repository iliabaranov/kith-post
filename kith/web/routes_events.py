"""Compose-a-card routes (G2): create/edit an event, preview it, serve its image."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from kith.config import SendMode, get_settings
from kith.core import calendar as cal
from kith.core import eventkind, images, wamessage
from kith.core import recipients as rcpt
from kith.core.cardstyles import CARD_STYLES, normalize_card_style
from kith.core.tracking import new_token
from kith.db.models import Asset, Event, Recipient, Reminder
from kith.services import contacts as book
from kith.services import scheduler, send, storage, waha
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


def _cc_json(cc_text: str, rsvp: bool) -> str | None:
    """Parse the CC field into encrypted-JSON storage. CC is only for cards without
    RSVP, so it's dropped when RSVP is on (defense in depth alongside the UI)."""
    if rsvp:
        return None
    parsed, _ = rcpt.parse_recipients(cc_text or "")
    if not parsed:
        return None
    return json.dumps([{"name": p.name, "email": p.email} for p in parsed])


def _cc_entries(ev: Event | None) -> list[dict]:
    if ev is None or not ev.cc:
        return []
    try:
        return json.loads(ev.cc)
    except (ValueError, TypeError):
        return []


def _cc_text(ev: Event | None) -> str:
    return "\n".join(
        (f"{c['name']} <{c['email']}>" if c.get("name") else c["email"])
        for c in _cc_entries(ev)
    )


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


# Why a WhatsApp batch stopped, in words a host can act on. Deliberately blames
# WhatsApp where WhatsApp is at fault, and never suggests re-linking during a
# timelock — that only makes the account look worse.
_WA_BLOCKED = {
    "timelock": (
        "WhatsApp has paused new conversations from your account, so those "
        "invitations weren't sent. They're still queued — try again once it lifts. "
        "Re-linking won't help and makes it look worse."
    ),
    "capped": (
        "You've used up WhatsApp's allowance for starting new conversations this "
        "cycle. Those invitations are still queued for the next one."
    ),
    "not-linked": (
        "Your WhatsApp isn't linked right now, so those invitations weren't sent. "
        "They're still queued."
    ),
    "unavailable": (
        "The WhatsApp service didn't answer, so those invitations weren't sent. "
        "They're still queued — check the server logs."
    ),
}


def _wa_send_preview(
    db: Session, ev: Event, user, rows: Sequence[Recipient]  # noqa: ANN001
) -> dict:
    """The message a WhatsApp guest will receive, and any quota caution."""
    settings = get_settings()
    queued_wa = [r for r in rows if r.phone and r.status == "queued"]
    if not settings.whatsapp_configured or not queued_wa:
        return {"wa_preview": None, "wa_quota_note": None}
    r = queued_wa[0]
    preview = wamessage.invite_text(
        title=ev.title,
        host_name=user.display_name or "A friend",
        view_url=f"{settings.base_url.rstrip('/')}/i/{r.token}",
        recipient_name=r.name,
        when=wamessage.when_line(ev.event_date, ev.event_time),
        rsvp=bool((ev.blocks or {}).get("rsvp")),
        invitation=eventkind.is_invitation(ev.blocks, ev.event_date),
    )
    # Warn before a big send, not after: WhatsApp caps how many *new* chats an
    # account may start per cycle, and the cap is what a party-sized list runs into.
    note = None
    cap = user.wa_capping or {}
    left = None
    if (
        isinstance(cap.get("total"), int)
        and isinstance(cap.get("used"), int)
        and cap["total"] >= 0          # -1 means the account has no cap
    ):
        left = max(0, cap["total"] - cap["used"])
    if cap.get("status") == "CAPPED":
        note = "WhatsApp won't let this account start new conversations this cycle."
    elif left is not None and left < len(queued_wa):
        note = (
            f"WhatsApp will only let you start {left} more conversation"
            f"{'' if left == 1 else 's'} this cycle, and {len(queued_wa)} are queued."
        )
    elif cap.get("status") in ("FIRST_WARNING", "SECOND_WARNING"):
        note = "You're near WhatsApp's limit on starting new conversations."
    return {"wa_preview": preview, "wa_quota_note": note}


def _wa_stuck_note(user, rows: Sequence[Recipient]) -> str | None:  # noqa: ANN001
    """The reason this card's WhatsApp invitations can't go out, if there is one."""
    settings = get_settings()
    if not settings.whatsapp_configured:
        return None
    if not any(r.phone and r.status == "queued" for r in rows):
        return None          # nothing waiting, so nothing to explain
    if not user.wa_session or user.wa_status != waha.STATUS_WORKING:
        return _WA_BLOCKED["not-linked"]
    if user.wa_timelock_until and _as_aware(user.wa_timelock_until) > datetime.now(UTC):
        return _WA_BLOCKED["timelock"]
    if (user.wa_capping or {}).get("status") == "CAPPED":
        return _WA_BLOCKED["capped"]
    return None


def _as_aware(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat them as the UTC they are."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _wa_batch_task(request: Request, ev: Event, result) -> BackgroundTask | None:  # noqa: ANN001
    """Hand an event's WhatsApp sends to a task that runs after the response.

    Messages are paced 5-20s apart, so a family-sized list takes minutes — far
    longer than an HTTP request should live, and longer than the tunnel will hold
    one open. Starlette runs a sync background task in a threadpool once the
    connection is closed, and recipients stay 'queued' until the batch reaches
    them, so the dashboard shows real progress on any refresh.
    """
    if not result.wa_pending:
        return None
    return BackgroundTask(
        send.send_whatsapp_batch,
        request.app.state.session_factory,
        ev.id,
        get_settings(),
    )


def _touch_book(db: Session, user_id: str, event_id: str) -> None:
    """Bump last_used_at for contacts this card just used.

    services.contacts.mark_used had no callers at all, so last_used_at was never
    set and the address book's "most recently used first" ordering silently
    sorted by creation date instead.
    """
    rows = db.execute(
        select(Recipient).where(Recipient.event_id == event_id)
    ).scalars().all()
    book.mark_used(
        db, user_id,
        [r.email for r in rows if r.email],
        [r.phone for r in rows if r.phone],
    )


def _needs_google(db: Session, event_id: str, statuses: tuple[str, ...]) -> bool:
    """Does this send actually need the Gmail connection?

    Only email recipients do. A card addressed entirely over WhatsApp must not be
    held hostage to a Google connection the host may never have made — and a host
    whose Google token has expired can still reach their WhatsApp guests.
    """
    rows = db.execute(
        select(Recipient).where(
            Recipient.event_id == event_id, Recipient.status.in_(statuses)
        )
    ).scalars().all()
    return any(not r.phone for r in rows)


def _wa_flags(user, settings) -> dict:  # noqa: ANN001 — a DB User + Settings
    """Whether to offer the WhatsApp box on the compose form.

    ``wa_ready`` = the channel is on and this host has a live session, so the box
    is usable. ``wa_offer`` = the channel is on but they haven't linked, so point
    them at it once rather than showing a field that can't send.
    """
    on = settings.whatsapp_configured
    linked = bool(user is not None and user.wa_session and user.wa_status == waha.STATUS_WORKING)
    return {"wa_ready": on and linked, "wa_offer": on and not linked}


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
        # A WhatsApp recipient carries email == "" (the NOT NULL sentinel), so the
        # address to show is whichever they actually have — otherwise an unnamed
        # WhatsApp guest rendered as a blank row.
        address = r.phone or r.email
        # WhatsApp's own receipts, kept as their own line rather than folded into
        # the status chip: "read" is the channel telling the host what it knows,
        # not evidence anyone opened the invitation.
        receipt = ""
        if r.phone:
            if r.wa_ack == -1:
                receipt = "WhatsApp couldn't deliver it"
            elif r.wa_read_at:
                receipt = "Read on WhatsApp"
            elif r.wa_delivered_at:
                receipt = "Delivered on WhatsApp"
        recipients.append({
            "name": r.name or address, "email": address,
            "channel": rcpt.CHANNEL_WHATSAPP if r.phone else rcpt.CHANNEL_EMAIL,
            "receipt": receipt,
            "state": state, "label": _STATE_LABEL[state],
            "party_size": r.party_size, "when": when,
            "adults": r.adults, "kids": r.kids,
            "note": r.note, "allergies": r.allergies,
        })
    return stats, recipients


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _send_ui(mode: str, queued_rows: list[Recipient], noun: str) -> tuple[str, str, str]:
    """Label, hint and confirmation for the send button.

    The wording names the channels this particular card will actually use. Saying
    "from your Gmail" over a card addressed entirely by WhatsApp is the kind of
    detail that makes a host distrust the button they're about to press.
    """
    n = len(queued_rows)
    wa = sum(1 for r in queued_rows if r.phone)
    em = n - wa
    if em and wa:
        via, dest = "by email and WhatsApp", "your Gmail and WhatsApp"
    elif wa:
        via, dest = "over WhatsApp", "your WhatsApp"
    else:
        via, dest = "by email", "your Gmail"
    thing = _plural(n, noun)

    if mode == "self-only":
        return (
            "Send a test to yourself",
            f"Sends only to you ({via}), for testing.",
            f"Send {thing} to yourself as a test?",
        )
    if mode == "live":
        return (
            f"Send to {n} now",
            f"Sends from {dest} to everyone still queued.",
            f"Send {thing} from {dest} now?",
        )
    return (
        "Send (dry run)",
        "Writes each message to data/outbox — nothing is really sent.",
        f"Write {thing} to the outbox as a dry run?",
    )


def _event_noun(ev) -> str:  # noqa: ANN001 — a DB Event
    """Host-facing noun: 'invitation' or 'card' (see core.eventkind)."""
    return eventkind.noun(ev.blocks, ev.event_date)


@dataclass
class ReconcileResult:
    added: int
    removed: int
    kept: int
    invalid: list[str]


def _reconcile_recipients(
    db: Session, event_id: str, text: str, phone_text: str = ""
) -> ReconcileResult:
    """Update an event's recipient list without discarding existing rows.

    Match by identity — the email, or "tel:<e164>" for a WhatsApp recipient: add
    new people (fresh token, queued, on the channel their box says), keep matched
    rows untouched (only fill an empty name), remove absent ones. Keeping rows
    preserves each recipient's token, sent/RSVP state, and mail threading — unlike
    a delete-and-recreate, which wiped all of that on every edit.

    The two boxes are reconciled together, so moving someone from one to the other
    is a remove plus an add: a different channel means a different conversation,
    and carrying an RSVP across would misreport which invite they answered.
    """
    valid, invalid = rcpt.parse_recipients(text)
    ph_valid, ph_invalid = rcpt.parse_phones(phone_text)
    valid, invalid = valid + ph_valid, invalid + ph_invalid
    existing = db.execute(
        select(Recipient).where(Recipient.event_id == event_id)
    ).scalars().all()
    by_id: dict[str, Recipient] = {}
    stale: list[Recipient] = []  # legacy duplicate rows for the same person
    for r in existing:
        key = rcpt.identity_of(r.email, r.phone)
        (stale.append(r) if key in by_id else by_id.setdefault(key, r))
    wanted = {p.identity: p for p in valid}  # already normalized

    added = kept = removed = 0
    for identity, p in wanted.items():
        row = by_id.get(identity)
        if row is None:
            db.add(Recipient(
                event_id=event_id, email=p.email, name=p.name, token=new_token(),
                channel=p.channel, phone=p.phone,
            ))
            added += 1
        else:
            if p.name and not row.name:  # fill a missing name, never overwrite
                row.name = p.name
            kept += 1
    for identity, row in by_id.items():
        if identity not in wanted:
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
        "blocks": DEFAULT_BLOCKS, "recipients_text": "", "wa_recipients_text": "",
        **_wa_flags(user, get_settings()),
        "cc_text": "", "error": None,
        "contacts": book.list_contacts(db, user.id),
        "card_styles": CARD_STYLES, "selected_style": normalize_card_style(None),
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
    card_style: str = Form("washi"),
    headcount_max: str = Form(""),
    timezone: str = Form(""),
    recipients: str = Form(""),
    wa_recipients: str = Form(""),
    cc: str = Form(""),
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
            "recipients_text": recipients, "wa_recipients_text": wa_recipients,
            "cc_text": cc, "error": str(e), **_wa_flags(user, get_settings()),
            "contacts": book.list_contacts(db, user.id),
            "card_styles": CARD_STYLES, "selected_style": normalize_card_style(card_style),
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
        card_style=normalize_card_style(card_style),
        cc=_cc_json(cc, bool(blocks.get("rsvp"))),
        blocks=blocks,
        headcount_max=_parse_int(headcount_max),
        timezone=(timezone.strip() or None),
        asset_id=asset.id if asset else None,
        status="draft",
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    _reconcile_recipients(db, ev.id, recipients, wa_recipients)
    _touch_book(db, user.id, ev.id)
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

    this_year = date.today().year

    def _fmt(dt) -> str:
        d = scheduler._as_utc(dt)
        if tz is not None:
            d = d.astimezone(tz)
        # Name the year when it isn't this one. The halfway slot for a distant
        # event lands years out, and without a year the list reads as though it
        # were out of order ("Jul 20" ahead of "Jun 7" — different years).
        fmt = "%a, %b %d at %I:%M %p" if d.year == this_year else "%a, %b %d %Y at %I:%M %p"
        return d.strftime(fmt).replace(" 0", " ")

    # One line per *time*, not per row. A reminder row is per recipient, and the
    # schedule is per event, so every waiting guest contributes an identical
    # timestamp — listing rows turned "three nudges planned" into thirty
    # indistinguishable lines. Group them and say how many people each covers.
    planned: list[dict] = []
    by_when: dict[str, dict] = {}
    for r in pending:
        when = _fmt(r.scheduled_for)
        slot = by_when.get(when)
        if slot is None:
            slot = {"when": when, "people": 0}
            by_when[when] = slot
            planned.append(slot)
        slot["people"] += 1

    return {
        "available": bool(ev.event_date) and bool((ev.blocks or {}).get("rsvp")),
        "enabled": cfg.enabled,
        # "sent" here means the event has actually gone out (so we don't tell the
        # host reminders will schedule "once you send" after they already have).
        "sent_any": any(r.status in ("sent", "coming", "declined") for r in recipients),
        "scheduled": len(pending),
        "sent": sum(1 for r in rows if r.status == "sent"),
        "planned": planned,
        # How many people are still waiting on a nudge, which is the number the
        # host actually wants — not how many rows are in the table.
        "awaiting": len({r.recipient_id for r in pending}),
        "schedule_desc": _describe_schedule(cfg),
        "max_per_recipient": cfg.max_per_recipient,
    }


@router.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(
    event_id: str, request: Request, db: Session = Depends(get_db),
    sent: int = 0, failed: int = 0, ask_contacts: int = 0, saved: int = 0,
    details_changed: int = 0, scheduled: int = 0, schedule_error: int = 0,
    wa_blocked: str = "", wa_pending: int = 0,
):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ev = _owned_event(db, user.id, event_id)
    if ev is None:
        return RedirectResponse("/", status_code=303)
    settings = get_settings()
    scheduled_display = None
    if ev.scheduled_send_at:
        d = scheduler._as_utc(ev.scheduled_send_at)
        tz = None
        if ev.timezone:
            try:
                tz = ZoneInfo(ev.timezone)
            except Exception:
                tz = None
        if tz is not None:
            d = d.astimezone(tz)
        scheduled_display = d.strftime("%a, %b %d at %I:%M %p").replace(" 0", " ")
    rows = db.execute(
        select(Recipient).where(Recipient.event_id == ev.id).order_by(Recipient.created_at)
    ).scalars().all()
    queued = sum(1 for r in rows if r.status == "queued")
    noun = _event_noun(ev)
    label, hint, confirm = _send_ui(
        settings.send_mode.value, [r for r in rows if r.status == "queued"], noun
    )
    # right after create/edit, offer to save recipients who aren't in the book yet
    new_contacts = 0
    if ask_contacts:
        parsed = [rcpt.Parsed(name=r.name, email=r.email, phone=r.phone) for r in rows]
        new_contacts = len(book.new_among(db, user.id, parsed))
    stats, recipients = _rsvp_summary(rows)
    resendable = sum(1 for r in rows if r.status in ("sent", "coming", "declined"))
    ctx = {
        "settings": settings, "user": user, "event": ev,
        "recipient_count": len(rows), "queued_count": queued,
        "send_mode": settings.send_mode.value,
        # The exact words a WhatsApp guest will get. Composed from a real
        # recipient, because a fake one would hide the personalisation — and the
        # host should be able to read it before it goes to their family.
        **_wa_send_preview(db, ev, user, rows),
        # Only suggest fixing Google when email is actually involved — a WhatsApp
        # card failing has nothing to do with a Gmail connection.
        "needs_google": any(not r.phone for r in rows if r.status == "queued"),
        "send_label": label, "send_hint": hint,
        "send_confirm": confirm, "noun": noun,
        "sent": sent, "failed": failed,
        # Why WhatsApp invitations are stuck, worked out from the account's
        # current state rather than echoed from the send that just happened: the
        # batch runs after the response now, so a timelock is discovered too late
        # to put in a redirect — and this stays true on every later page load.
        "wa_blocked_msg": _wa_stuck_note(user, rows) or _WA_BLOCKED.get(wa_blocked),
        "wa_sending": send.wa_batch_running(ev.id) or bool(wa_pending),
        "wa_pending": wa_pending,
        "new_contacts": new_contacts, "saved": saved,
        "stats": stats, "recipients": recipients,
        "reminders": _reminders_ui(db, ev, settings, rows),
        "details_changed": bool(details_changed), "resendable": resendable,
        "scheduled_display": scheduled_display,
        "scheduled": bool(scheduled), "schedule_error": bool(schedule_error),
        "cc_list": [c.get("name") or c["email"] for c in _cc_entries(ev)],
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
    if (
        settings.send_mode != SendMode.dry_run
        and not user.refresh_token
        and _needs_google(db, ev.id, ("queued",))
    ):
        return RedirectResponse(f"/events/{ev.id}?failed=1", status_code=303)
    result = send.send_event(db, ev, user, settings, wa_defer=True)
    scheduler.schedule_event_reminders(db, ev, settings)  # nudge non-responders (G5)
    ev.scheduled_send_at = None  # sending now supersedes any pending schedule
    db.commit()
    url = f"/events/{ev.id}?sent={result.sent}&failed={result.failed}"
    if result.wa_blocked:
        # A WhatsApp batch stopped as a whole needs explaining, or the host just
        # sees invitations that didn't go anywhere.
        url += f"&wa_blocked={result.wa_blocked}"
    if result.wa_pending:
        url += f"&wa_pending={result.wa_pending}"
    return RedirectResponse(url, status_code=303, background=_wa_batch_task(request, ev, result))


def _schedule_to_utc(d: date | None, hhmm: str, tzname: str | None) -> datetime | None:
    """Combine a date + 'HH:MM' in the host's tz into an aware UTC datetime."""
    t = cal.parse_hhmm(hhmm)
    if d is None or t is None:
        return None
    naive = datetime(d.year, d.month, d.day, t.hour, t.minute)
    return cal.to_utc(naive, tzname) if tzname else naive.replace(tzinfo=UTC)


@router.post("/events/{event_id}/schedule")
def schedule_send(
    event_id: str, request: Request, db: Session = Depends(get_db),
    send_date: str = Form(""), send_time: str = Form(""), timezone: str = Form(""),
):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ev = _owned_event(db, user.id, event_id)
    if ev is None:
        return RedirectResponse("/", status_code=303)
    tzname = (timezone.strip() or ev.timezone or None)
    when = _schedule_to_utc(_parse_date(send_date), send_time.strip(), tzname)
    queued = db.execute(
        select(func.count()).select_from(Recipient).where(
            Recipient.event_id == ev.id, Recipient.status == "queued"
        )
    ).scalar_one()
    if when is None or when <= datetime.now(UTC) or queued == 0:
        return RedirectResponse(f"/events/{ev.id}?schedule_error=1", status_code=303)
    ev.scheduled_send_at = when
    if timezone.strip():
        ev.timezone = timezone.strip()  # keep tz for the calendar links too
    db.commit()
    return RedirectResponse(f"/events/{ev.id}?scheduled=1", status_code=303)


@router.post("/events/{event_id}/unschedule")
def unschedule_send(event_id: str, request: Request, db: Session = Depends(get_db)):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ev = _owned_event(db, user.id, event_id)
    if ev is None:
        return RedirectResponse("/", status_code=303)
    ev.scheduled_send_at = None
    db.commit()
    return RedirectResponse(f"/events/{ev.id}", status_code=303)


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
    if (
        settings.send_mode != SendMode.dry_run
        and not user.refresh_token
        and _needs_google(db, ev.id, ("sent", "coming", "declined"))
    ):
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
        db, ev, user, settings, note="Some details have changed — here's the latest.",
        wa_defer=True,
    )
    scheduler.schedule_event_reminders(db, ev, settings)
    url = f"/events/{ev.id}?sent={result.sent}&failed={result.failed}"
    if result.wa_pending:
        url += f"&wa_pending={result.wa_pending}"
    return RedirectResponse(url, status_code=303, background=_wa_batch_task(request, ev, result))


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
    added = sum(
        book.add_contact(db, user.id, r.email, r.name, phone=r.phone)[1] for r in rows
    )
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

    def _lines(people, addr) -> str:
        return "\n".join((f"{r.name} <{addr(r)}>" if r.name else addr(r)) for r in people)

    recipients_text = _lines([r for r in rows if not r.phone], lambda r: r.email)
    wa_recipients_text = _lines([r for r in rows if r.phone], lambda r: r.phone)
    ctx = {
        "settings": get_settings(), "user": user, "event": ev,
        "blocks": ev.blocks or DEFAULT_BLOCKS, "recipients_text": recipients_text,
        "wa_recipients_text": wa_recipients_text,
        "cc_text": _cc_text(ev), "error": None, **_wa_flags(user, get_settings()),
        "contacts": book.list_contacts(db, user.id),
        "card_styles": CARD_STYLES, "selected_style": normalize_card_style(ev.card_style),
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
    card_style: str = Form("washi"),
    headcount_max: str = Form(""),
    timezone: str = Form(""),
    recipients: str = Form(""),
    wa_recipients: str = Form(""),
    cc: str = Form(""),
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
            "recipients_text": recipients, "wa_recipients_text": wa_recipients,
            "cc_text": cc, "error": str(e), **_wa_flags(user, get_settings()),
            "contacts": book.list_contacts(db, user.id),
            "card_styles": CARD_STYLES, "selected_style": normalize_card_style(card_style),
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
    ev.card_style = normalize_card_style(card_style)
    ev.cc = _cc_json(cc, bool(blocks.get("rsvp")))
    ev.headcount_max = _parse_int(headcount_max)
    ev.timezone = (timezone.strip() or None)
    ev.blocks = blocks
    if derived is not None:
        ev.asset_id = storage.store_asset(db, user.id, derived).id
    details_changed = before != (ev.event_date, ev.event_time, ev.event_end_time, ev.location)
    db.commit()
    _reconcile_recipients(db, ev.id, recipients, wa_recipients)
    _touch_book(db, user.id, ev.id)
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
        "card_style": normalize_card_style(ev.card_style),
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
    # asset id is content-stable (a new image gets a new id), so cache hard.
    return FileResponse(
        path, media_type=asset.mime,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )
