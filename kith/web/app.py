"""Kith Post web app.

G0 landing + dev loop · G1 Google sign-in + encrypted accounts · G2 compose a card.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from kith.config import get_settings
from kith.db.models import Contact, Event, Recipient, User
from kith.db.session import init_db, make_engine, make_session_factory
from kith.services import google_auth, scheduler, storage
from kith.services.google_auth import GoogleIdentity
from kith.web.deps import WEB_DIR, get_db, load_user, templates
from kith.web.ratelimit import limiter
from kith.web.routes_contacts import router as contacts_router
from kith.web.routes_events import router as events_router
from kith.web.routes_invite import router as invite_router

log = logging.getLogger("kith")

# Python's default mimetypes doesn't know woff2/woff; register so self-hosted
# fonts are served as font/woff2 rather than application/octet-stream.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")


class CachedStaticFiles(StaticFiles):
    """StaticFiles that sets long-lived cache headers. URLs carrying a ``?v=``
    cache-buster (our CSS/JS use ``?v=<mtime>``) are safe to cache forever;
    everything else (e.g. the unversioned favicon) gets a modest one-hour TTL."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            versioned = b"v=" in scope.get("query_string", b"")
            # font files have content-unique names and never change in place
            is_font = path.endswith((".woff2", ".woff", ".ttf", ".otf"))
            if versioned or is_font:
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.outbox_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.db_path)
    init_db(engine)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    # Reminder sweep: an in-process background task. Guarded on reminder config so
    # it stays inert until that config is wired (and off when disabled).
    app.state.sweep_task = None
    rcfg = getattr(settings, "reminders", None)
    # A single maintenance loop drives reminders AND asset purge, so it runs
    # whenever the sweep interval is set (reminders may be off but purge still due).
    interval = getattr(rcfg, "sweep_seconds", 0) if rcfg is not None else 0
    if interval > 0:
        app.state.sweep_task = asyncio.create_task(scheduler.sweep_loop(app, settings, interval))
    try:
        yield
    finally:
        if app.state.sweep_task is not None:
            app.state.sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await app.state.sweep_task
        engine.dispose()


def upsert_user(db: Session, identity: GoogleIdentity) -> User:
    user = db.execute(
        select(User).where(User.google_sub == identity.sub)
    ).scalar_one_or_none()
    if user is None:
        user = User(google_sub=identity.sub, email=identity.email, display_name=identity.name)
        db.add(user)
    else:
        user.email = identity.email
        if identity.name:
            user.display_name = identity.name
    if identity.refresh_token:
        user.refresh_token = identity.refresh_token
        user.reconnect_needed = False  # fresh consent restored a working token
    user.last_login_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)
    return user


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=settings.https_only,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "DENY")  # clickjacking
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        # only send the origin (not the /i/<token> path) to external links
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        return resp
    app.mount("/static", CachedStaticFiles(directory=str(WEB_DIR / "static")), name="static")
    app.include_router(events_router)
    app.include_router(invite_router)
    app.include_router(contacts_router)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request):
        return templates.TemplateResponse(request, "privacy.html", {"settings": settings})

    @app.get("/terms", response_class=HTMLResponse)
    def terms(request: Request):
        return templates.TemplateResponse(request, "terms.html", {"settings": settings})

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, db: Session = Depends(get_db)):
        user = load_user(request, db)
        events = []
        counts: dict[str, int] = {}
        sent_at: dict[str, datetime] = {}
        scheduled_disp: dict[str, str] = {}
        if user is not None:
            events = db.execute(
                select(Event).where(Event.user_id == user.id).order_by(Event.created_at.desc())
            ).scalars().all()
            if events:
                ids = [e.id for e in events]
                counts = dict(
                    db.execute(
                        select(Recipient.event_id, func.count())
                        .where(Recipient.event_id.in_(ids))
                        .group_by(Recipient.event_id)
                    ).all()
                )
                # earliest send time per event → "Sent <date>" for dateless cards
                for eid, ts in db.execute(
                    select(Recipient.event_id, Recipient.sent_at)
                    .where(Recipient.event_id.in_(ids), Recipient.sent_at.is_not(None))
                ).all():
                    if ts is not None and (eid not in sent_at or ts < sent_at[eid]):
                        sent_at[eid] = ts
                # scheduled send time as MM/DD/YY, in the card's own timezone
                for e in events:
                    if not e.scheduled_send_at:
                        continue
                    d = e.scheduled_send_at
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=UTC)
                    tz = None
                    if e.timezone:
                        try:
                            tz = ZoneInfo(e.timezone)
                        except Exception:
                            tz = None
                    if tz is not None:
                        d = d.astimezone(tz)
                    scheduled_disp[e.id] = d.strftime("%m/%d/%y")
        ctx = {
            "settings": settings, "user": user, "events": events,
            "counts": counts, "today": date.today(), "sent_at": sent_at,
            "scheduled_disp": scheduled_disp,
        }
        return templates.TemplateResponse(request, "index.html", ctx)

    # ---- auth ----
    @app.get("/auth/login")
    @limiter.limit("15/minute")
    def login(request: Request):
        if settings.google_configured:
            url, state, code_verifier = google_auth.authorization_url(settings)
            request.session["oauth_state"] = state
            request.session["oauth_verifier"] = code_verifier
            return RedirectResponse(url)
        return templates.TemplateResponse(request, "login_dev.html", {"settings": settings})

    def _no_access(request: Request, email: str = "") -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "no_access.html",
            {"settings": settings, "attempted_email": email},
            status_code=403,
        )

    @app.get("/auth/callback")
    @limiter.limit("15/minute")
    def callback(
        request: Request,
        db: Session = Depends(get_db),
        code: str = "",
        state: str = "",
        error: str = "",
    ):
        # Google refused or the user cancelled (e.g. ?error=access_denied). In an
        # invite-only app that usually means "not on the list" — point them at the
        # host rather than dumping them back on the homepage.
        if error:
            log.info("OAuth callback returned error=%s", error)
            return _no_access(request)
        expected = request.session.pop("oauth_state", None)
        verifier = request.session.pop("oauth_verifier", None)
        if not code or (expected and state != expected):
            return RedirectResponse("/?error=auth", status_code=303)
        try:
            identity = google_auth.exchange_code(settings, code, state, verifier)
        except Exception:
            log.exception("OAuth token exchange failed")
            return RedirectResponse("/?error=auth", status_code=303)
        # App-level whitelist, in addition to Google's test-user list. Matters when
        # the app is "in production (unverified)", where anyone can authenticate.
        if not settings.email_allowed(identity.email):
            log.info("Sign-in blocked: address not on the allowlist")
            return _no_access(request, identity.email)
        user = upsert_user(db, identity)
        request.session["user_id"] = user.id
        return RedirectResponse("/", status_code=303)

    @app.post("/auth/dev-login")
    def dev_login(request: Request, db: Session = Depends(get_db)):
        if settings.google_configured:
            return RedirectResponse("/auth/login", status_code=303)
        identity = GoogleIdentity(
            sub="dev-user", email="dev@example.com", name="Dev User", refresh_token=None
        )
        user = upsert_user(db, identity)
        request.session["user_id"] = user.id
        return RedirectResponse("/", status_code=303)

    @app.post("/auth/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    # ---- account ----
    @app.get("/account", response_class=HTMLResponse)
    def account(request: Request, db: Session = Depends(get_db)):
        user = load_user(request, db)
        if user is None:
            return RedirectResponse("/", status_code=303)
        ctx = {"settings": settings, "user": user}
        return templates.TemplateResponse(request, "account.html", ctx)

    @app.get("/account/export")
    def export(request: Request, db: Session = Depends(get_db)):
        user = load_user(request, db)
        if user is None:
            return RedirectResponse("/", status_code=303)
        events = db.execute(select(Event).where(Event.user_id == user.id)).scalars().all()
        contacts = db.execute(select(Contact).where(Contact.user_id == user.id)).scalars().all()
        data = {
            "id": user.id,
            "google_sub": user.google_sub,
            "email": user.email,
            "display_name": user.display_name,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
            "contacts": [{"name": c.name, "email": c.email} for c in contacts],
            "events": [
                {
                    "id": e.id, "title": e.title, "message": e.message,
                    "event_date": e.event_date.isoformat() if e.event_date else None,
                    "event_time": e.event_time, "event_end_time": e.event_end_time,
                    "location": e.location, "signoff": e.signoff, "blocks": e.blocks,
                    "headcount_max": e.headcount_max, "timezone": e.timezone,
                }
                for e in events
            ],
        }
        return JSONResponse(
            data, headers={"Content-Disposition": "attachment; filename=kith-post-export.json"}
        )

    @app.post("/account/delete")
    def delete_account(request: Request, db: Session = Depends(get_db)):
        user = load_user(request, db)
        if user is not None:
            storage.delete_user_assets(user.id)  # remove image files (DB rows cascade)
            db.delete(user)
            db.commit()
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    return app


app = create_app()
