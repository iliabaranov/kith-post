"""Invitation card frame styles — the per-card background/frame treatment.

Each value maps to an ``f-<value>`` CSS class on the invitation card (see
invite.css) and a swatch in the card editor. NULL/unknown falls back to the
default so every existing card keeps the original look.
"""

from __future__ import annotations

# value -> (label, one-line description); insertion order = picker order
CARD_STYLES: dict[str, tuple[str, str]] = {
    "washi": ("Washi Tape", "Tilted, taped — the classic look"),
    "clean": ("Clean", "Straight and rounded, no fuss"),
    "corners": ("Photo Corners", "Mounted like an album print"),
    "postmark": ("Postmark", "A postcard with a cancellation stamp"),
    "matte": ("Matte Frame", "An inset mat, for dressier events"),
}
DEFAULT_CARD_STYLE = "washi"


def normalize_card_style(value: str | None) -> str:
    """Coerce arbitrary input to a known style, defaulting to washi."""
    v = (value or "").strip().lower()
    return v if v in CARD_STYLES else DEFAULT_CARD_STYLE
