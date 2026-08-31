"""Lazy thumbnail generation for ticket images.

Ticket photos are uploaded straight from phones and typically weigh ~850 KB
each. Serving them at full size inside the dashboard list wastes a lot of
bandwidth on a slow link, so a downscaled JPEG is derived from every original
and cached next to it. Thumbnails are generated at upload time and again on
demand, which also covers images uploaded before this module existed.

The originals are deliberately left untouched: zooming in still shows them at
full quality.
"""

from __future__ import annotations

import logging
from pathlib import Path

LOGGER = logging.getLogger("score_fourfold.thumbnails")

THUMB_SUBDIR = "thumbs"
THUMB_MAX_EDGE = 400
THUMB_QUALITY = 78


def thumb_dir(ticket_image_dir: Path) -> Path:
    """Return the directory holding derived thumbnails for ``ticket_image_dir``."""
    return ticket_image_dir / THUMB_SUBDIR


def thumb_name(filename: str) -> str:
    """Map an original image filename to its cached thumbnail file name."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return f"{stem}.jpg"


def ensure_thumbnail(source: Path, target: Path) -> bytes | None:
    """Return thumbnail bytes for ``source``, generating and caching on a miss.

    Returns ``None`` when Pillow is unavailable or the source cannot be
    decoded; callers are expected to fall back to the original image so the
    dashboard never renders a broken thumbnail.
    """
    if target.is_file():
        try:
            return target.read_bytes()
        except OSError:
            LOGGER.warning("读取缩略图失败，将重新生成: %s", target)
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - depends on the runtime environment
        LOGGER.warning("Pillow 未安装，无法生成缩略图")
        return None
    try:
        with Image.open(source) as opened:
            # Phone photos carry an EXIF orientation that browsers apply to the
            # full-size image; transpose so the thumbnail is not rotated a
            # different way from the picture the user sees when zooming in.
            image = ImageOps.exif_transpose(opened)
            image.load()
            image.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE))
            if image.mode != "RGB":
                image = image.convert("RGB")
            target.parent.mkdir(parents=True, exist_ok=True)
            image.save(target, "JPEG", quality=THUMB_QUALITY, optimize=True)
    except (OSError, ValueError) as exc:
        # Truncated or malformed uploads are an expected outcome; warn without
        # a stack trace so ordinary bad files do not flood the log.
        LOGGER.warning("跳过缩略图生成（图片可能损坏或目录不可写）: %s (%s)", source.name, exc)
        return None
    except Exception:
        LOGGER.exception("生成缩略图失败: %s", source)
        return None
    try:
        return target.read_bytes()
    except OSError:
        LOGGER.warning("缩略图写入后读取失败: %s", target)
        return None
