"""Asset storage on the local data volume."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from kith.config import get_settings
from kith.core.images import Derived
from kith.db.models import Asset


def _assets_root() -> Path:
    return get_settings().data_dir / "assets"


def store_asset(db: Session, user_id: str, d: Derived) -> Asset:
    asset_id = uuid.uuid4().hex
    adir = _assets_root() / user_id / asset_id
    adir.mkdir(parents=True, exist_ok=True)
    full_path = adir / f"full.{d.ext}"
    inline_path = adir / f"inline.{d.ext}"
    full_path.write_bytes(d.full)
    inline_path.write_bytes(d.inline)
    asset = Asset(
        id=asset_id,
        user_id=user_id,
        sha256=d.sha256,
        mime=d.mime,
        full_path=str(full_path),
        inline_path=str(inline_path),
        width=d.width,
        height=d.height,
        bytes=d.src_bytes,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


def delete_user_assets(user_id: str) -> None:
    """Remove a user's asset files from disk (DB rows cascade separately)."""
    shutil.rmtree(_assets_root() / user_id, ignore_errors=True)


def delete_asset(db: Session, asset: Asset) -> None:
    """Remove one asset's files + row — used when its event is deleted. Each
    upload makes its own Asset, so an event's asset is never shared."""
    shutil.rmtree(Path(asset.full_path).parent, ignore_errors=True)
    db.delete(asset)
    db.commit()
