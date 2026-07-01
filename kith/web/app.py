"""Kith Post web app.

G0 landing + dev loop · G1 Google sign-in + encrypted accounts · G2 compose a card.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from kith.config import get_settings
from kith.db.models import Contact, Event, Recipient, User
from kith.db.session import init_db, make_engine, make_session_factory
from kith.services import google_auth, storage
from kith.services.google_auth import GoogleIdentity
from kith.web.deps import WEB_DIR, get_db, load_user, templates
from kith.web.routes_contacts import router as contacts_router
from kith.web.routes_events import router as events_router
from kith.web.routes_invite import router as invite_router

log = logging.getLogger("kith")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.outbox_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.db_path)
    init_db(engine)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    try:
        yield
    finally:
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
    user.last_login_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)
    return user


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=settings.https_only,
    )
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    app.include_router(events_router)
    app.include_router(invite_router)
    app.include_router(contacts_router)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, db: Session = Depends(get_db)):
        user = load_user(request, db)
        events = []
        counts: dict[str, int] = {}
        if user is not None:
            events = db.execute(
                select(Event).where(Event.user_id == user.id).order_by(Event.created_at.desc())
            ).scalars().all()
            if events:
                counts = dict(
                    db.execute(
                        select(Recipient.event_id, func.count())
                        .where(Recipient.event_id.in_([e.id for e in events]))
                        .group_by(Recipient.event_id)
                    ).all()
                )
        ctx = {
            "settings": settings, "user": user, "events": events,
            "counts": counts, "today": date.today(),
        }
        return templates.TemplateResponse(request, "index.html", ctx)

    # ---- auth ----
    @app.get("/auth/login")
    def login(request: Request):
        if settings.google_configured:
            url, state, code_verifier = google_auth.authorization_url(settings)
            request.session["oauth_state"] = state
            request.session["oauth_verifier"] = code_verifier
            return RedirectResponse(url)
        return templates.TemplateResponse(request, "login_dev.html", {"settings": settings})

    @app.get("/auth/callback")
    def callback(request: Request, db: Session = Depends(get_db), code: str = "", state: str = ""):
        expected = request.session.pop("oauth_state", None)
        verifier = request.session.pop("oauth_verifier", None)
        if not code or (expected and state != expected):
            return RedirectResponse("/?error=auth", status_code=303)
        try:
            identity = google_auth.exchange_code(settings, code, state, verifier)
        except Exception:
            log.exception("OAuth token exchange failed")
            return RedirectResponse("/?error=auth", status_code=303)
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

    @app.get("/auth/logout")
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
