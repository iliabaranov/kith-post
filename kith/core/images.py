"""Card image processing — pure: bytes in, sanitized derivatives out.

- Validates type + size.
- Strips EXIF/GPS (re-encodes; bakes in orientation first so photos stay upright).
- Produces a full-res copy (capped) for the landing page and a smaller "inline"
  copy for embedding in the email.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageOps

ALLOWED_INPUT_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "MPO"}
DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_PIXELS = 40_000_000  # ~40 MP — reject decompression bombs before decoding
# Backstop: Pillow raises DecompressionBombError past 2x this while decoding.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
FULL_EDGE = 2000   # full-res long edge (landing page)
INLINE_EDGE = 1100  # inline long edge (email CID)
JPEG_QUALITY = 82


class ImageError(ValueError):
    """A user-friendly problem with an uploaded image."""


@dataclass(frozen=True)
class Derived:
    full: bytes
    inline: bytes
    mime: str
    ext: str
    width: int   # full-res dimensions
    height: int
    sha256: str  # of the original upload
    src_bytes: int


def _fit(img: Image.Image, edge: int) -> Image.Image:
    w, h = img.size
    if max(w, h) <= edge:
        return img
    if w >= h:
        nw, nh = edge, max(1, round(h * edge / w))
    else:
        nh, nw = edge, max(1, round(w * edge / h))
    return img.resize((nw, nh), Image.LANCZOS)


def process(data: bytes, *, max_bytes: int = DEFAULT_MAX_BYTES) -> Derived:
    if not data:
        raise ImageError("That file is empty.")
    if len(data) > max_bytes:
        raise ImageError(f"That image is larger than {max_bytes // (1024 * 1024)} MB.")
    try:
        img = Image.open(io.BytesIO(data))
        # Check declared dimensions from the header BEFORE decoding, so a small but
        # highly-compressed "bomb" can't blow up memory in img.load().
        if img.width * img.height > MAX_PIXELS:
            raise ImageError("That image has too many megapixels — try a smaller one.")
        img.load()
    except ImageError:
        raise
    except Image.DecompressionBombError as e:
        raise ImageError("That image is too large to process.") from e
    except Exception as e:
        raise ImageError("That doesn't look like an image.") from e
    if (img.format or "").upper() not in ALLOWED_INPUT_FORMATS:
        raise ImageError("That image type isn't supported — try JPG, PNG, or WebP.")

    img = ImageOps.exif_transpose(img)  # apply orientation, then drop EXIF on re-encode

    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if has_alpha:
        img = img.convert("RGBA")
        out_fmt, mime, ext = "PNG", "image/png", "png"
    else:
        img = img.convert("RGB")
        out_fmt, mime, ext = "JPEG", "image/jpeg", "jpg"

    full_img = _fit(img, FULL_EDGE)
    inline_img = _fit(img, INLINE_EDGE)

    def encode(im: Image.Image) -> bytes:
        buf = io.BytesIO()
        if out_fmt == "JPEG":
            im.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)  # no exif= -> stripped
        else:
            im.save(buf, "PNG", optimize=True)
        return buf.getvalue()

    return Derived(
        full=encode(full_img),
        inline=encode(inline_img),
        mime=mime,
        ext=ext,
        width=full_img.width,
        height=full_img.height,
        sha256=hashlib.sha256(data).hexdigest(),
        src_bytes=len(data),
    )
