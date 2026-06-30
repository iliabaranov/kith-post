"""Kith Post web app (G0 scaffold).

Serves the warm "Kitchen Table" landing page and a health check, and wires up the
SQLite data volume on startup. Google SSO, events, sending, and tracking arrive
in later gates.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from kith.config import get_settings
from kith.db.session import init_db, make_engine

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.outbox_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.db_path)
    init_db(engine)
    app.state.engine = engine
    try:
        yield
    finally:
        engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request, "index.html", {"settings": settings})

    @app.get("/auth/login", response_class=HTMLResponse)
    def login(request: Request):
        # Google SSO arrives in G1; this keeps the landing CTA non-broken.
        return templates.TemplateResponse(request, "soon.html", {"settings": settings})

    return app


app = create_app()
