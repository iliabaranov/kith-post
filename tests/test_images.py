import hashlib
import io

import pytest
from PIL import Image

from kith.core.images import ImageError, process


def _png(size=(40, 30), color="red", mode="RGB") -> bytes:
    b = io.BytesIO()
    Image.new(mode, size, color).save(b, "PNG")
    return b.getvalue()


def _jpeg_with_orientation() -> bytes:
    img = Image.new("RGB", (40, 30), "blue")
    exif = img.getexif()
    exif[274] = 6  # EXIF Orientation tag
    b = io.BytesIO()
    img.save(b, "JPEG", exif=exif)
    return b.getvalue()


def test_rejects_non_image():
    with pytest.raises(ImageError):
        process(b"definitely not an image")


def test_rejects_empty():
    with pytest.raises(ImageError):
        process(b"")


def test_rejects_oversize():
    with pytest.raises(ImageError):
        process(_png(), max_bytes=10)


def test_opaque_image_becomes_jpeg():
    d = process(_png())
    assert d.mime == "image/jpeg" and d.ext == "jpg"


def test_alpha_image_stays_png():
    d = process(_png(mode="RGBA", color=(255, 0, 0, 120)))
    assert d.mime == "image/png" and d.ext == "png"


def test_large_image_is_capped():
    d = process(_png(size=(3000, 1500)))
    full = Image.open(io.BytesIO(d.full))
    inline = Image.open(io.BytesIO(d.inline))
    assert max(full.size) <= 2000
    assert max(inline.size) <= 1100


def test_exif_is_stripped():
    d = process(_jpeg_with_orientation())
    out = Image.open(io.BytesIO(d.full))
    assert dict(out.getexif()) == {}  # no orientation/GPS/etc. survives


def test_sha256_is_of_original():
    data = _png()
    assert process(data).sha256 == hashlib.sha256(data).hexdigest()
