from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


PREFERRED_PREFIXES = ("ref", "main")


def list_images(folder: Path) -> list[Path]:
    """Product photos; files named ref*/main* (the clearest product shots) come first,
    since only the first few are sent to VideoScript / Muse."""
    return sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES),
        key=lambda p: (not p.name.lower().startswith(PREFERRED_PREFIXES), p.name.lower()),
    )


def shrink_to_jpeg(source: Path, max_dim: int = 1280, quality: int = 86) -> bytes:
    """Re-encode an image as a compact JPEG so several fit in one Muse message."""
    with Image.open(source) as image:
        image.load()
        image.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        if image.mode not in ("RGB", "L"):
            background = Image.new("RGB", image.size, (255, 255, 255))
            rgba = image.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            image = background
        out = io.BytesIO()
        image.convert("RGB").save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()


def shrink_copy(source: Path, target: Path, max_dim: int = 1280) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(shrink_to_jpeg(source, max_dim=max_dim))
    return target


def data_url(source: Path, max_dim: int = 1600) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(
        shrink_to_jpeg(source, max_dim=max_dim)
    ).decode("ascii")
