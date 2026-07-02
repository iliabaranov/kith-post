"""Shared web helpers: templates, DB session dependency, current-user lookup."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from kith.core.calendar import pretty_time
from kith.db.models import User

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.filters["fmt_time"] = pretty_time  # "15:00" -> "3:00 pm"


def static_version() -> str:
    """Cache-busting token = newest mtime under static/. Appended as ?v= to
    CSS/JS links so a changed asset is refetched without a manual hard-refresh,
    while unchanged assets stay cached. Cheap (a handful of files)."""
    root = WEB_DIR / "static"
    latest = max((p.stat().st_mtime for p in root.rglob("*") if p.is_file()), default=0.0)
    return str(int(latest))


templates.env.globals["static_v"] = static_version


_css_cache: dict[str, tuple[float, str]] = {}


def inline_css(name: str) -> str:
    """Contents of static/css/<name>, for inlining into a <style> block so the
    critical CSS (and @font-face rules) ship in the HTML — no render-blocking
    stylesheet request and no extra hop before fonts start loading. Cached per
    file mtime, so edits are picked up without a restart. Use with the |safe
    filter (trusted, first-party files only)."""
    path = WEB_DIR / "static" / "css" / name
    mtime = path.stat().st_mtime
    cached = _css_cache.get(name)
    if cached is None or cached[0] != mtime:
        _css_cache[name] = (mtime, path.read_text())
    return _css_cache[name][1]


templates.env.globals["inline_css"] = inline_css


def get_db(request: Request):
    db: Session = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def load_user(request: Request, db: Session) -> User | None:
    uid = request.session.get("user_id")
    return db.get(User, uid) if uid else None
