"""Kith Post web app.

G0: landing + dev loop. G1: Google sign-in (with a dev-login fallback when Google
isn't configured), encrypted refresh-token storage, account export/delete.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from kith.config import get_settings
from kith.db.models import User
from kith.db.session import init_db, make_engine, make_session_factory
from kith.services import google_auth
from kith.services.google_auth import GoogleIdentity

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
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


def get_db(request: Request):
    db: Session = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def load_user(request: Request, db: Session) -> User | None:
    uid = request.session.get("user_id")
    return db.get(User, uid) if uid else None


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

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, db: Session = Depends(get_db)):
        user = load_user(request, db)
        ctx = {"settings": settings, "user": user}
        return templates.TemplateResponse(request, "index.html", ctx)

    # ---- auth ----
    @app.get("/auth/login")
    def login(request: Request):
        if settings.google_configured:
            url, state, code_verifier = google_auth.authorization_url(settings)
            request.session["oauth_state"] = state
            request.session["oauth_verifier"] = code_verifier
            return RedirectResponse(url)
        # No Google creds → local dev sign-in so the signed-in app is testable.
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
        if settings.google_configured:  # dev login is disabled once real OAuth is set up
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
        data = {
            "id": user.id,
            "google_sub": user.google_sub,
            "email": user.email,
            "display_name": user.display_name,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        }
        return JSONResponse(
            data, headers={"Content-Disposition": "attachment; filename=kith-post-export.json"}
        )

    @app.post("/account/delete")
    def delete_account(request: Request, db: Session = Depends(get_db)):
        user = load_user(request, db)
        if user is not None:
            db.delete(user)
            db.commit()
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    return app


app = create_app()
