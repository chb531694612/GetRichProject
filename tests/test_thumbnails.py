"""Regression tests for ticket image thumbnails.

The dashboard list renders every plan's ticket photo. Those are uploaded
straight from phones (~850 KB each), so the list must never download the
originals; only the zoomed view may.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from pathlib import Path

from score_fourfold.thumbnails import (
    THUMB_MAX_EDGE,
    ensure_thumbnail,
    thumb_dir,
    thumb_name,
)

try:
    from PIL import Image
except ImportError:  # pragma: no cover - depends on the runtime environment
    Image = None  # type: ignore[assignment]


def _photo(width: int = 1600, height: int = 1200) -> bytes:
    """Build a noisy JPEG so compression behaves like a real photo."""
    if Image is None:  # pragma: no cover
        raise unittest.SkipTest("Pillow is required for thumbnail tests")
    buffer = io.BytesIO()
    Image.effect_noise((width, height), 48).convert("RGB").save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


class ThumbnailNamingTests(unittest.TestCase):
    def test_thumb_name_maps_every_source_extension_to_jpeg(self):
        stem = "BF4-20260828-a906b9e5fa"
        for ext in ("jpg", "png", "gif", "webp"):
            self.assertEqual(thumb_name(f"{stem}.{ext}"), f"{stem}.jpg")

    def test_thumb_name_handles_a_missing_extension(self):
        self.assertEqual(thumb_name("noextension"), "noextension.jpg")

    def test_thumb_dir_is_a_subdirectory_of_the_image_dir(self):
        self.assertEqual(thumb_dir(Path("/data/ticket-images")), Path("/data/ticket-images/thumbs"))


class ThumbnailGenerationTests(unittest.TestCase):
    def setUp(self):
        if Image is None:  # pragma: no cover
            self.skipTest("Pillow is required for thumbnail tests")
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _target(self, filename: str) -> Path:
        return thumb_dir(self.tmp) / thumb_name(filename)

    def test_generates_downscaled_jpeg_and_caches_it(self):
        source = self.tmp / "plan.jpg"
        source.write_bytes(_photo(1600, 1200))
        target = self._target("plan.jpg")
        data = ensure_thumbnail(source, target)
        self.assertIsNotNone(data)
        self.assertTrue(target.is_file(), "缩略图应缓存到磁盘")
        self.assertEqual(target.read_bytes(), data)
        with Image.open(io.BytesIO(data)) as thumb:
            self.assertEqual(thumb.format, "JPEG")
            self.assertLessEqual(max(thumb.size), THUMB_MAX_EDGE)

    def test_thumbnail_is_far_smaller_than_the_original(self):
        source = self.tmp / "plan.jpg"
        raw = _photo(2000, 1500)
        source.write_bytes(raw)
        data = ensure_thumbnail(source, self._target("plan.jpg"))
        self.assertIsNotNone(data)
        self.assertLess(len(data), len(raw) // 4, "缩略图应远小于原图，否则列表省流量的目的落空")

    def test_second_call_serves_the_cached_file_without_touching_the_source(self):
        source = self.tmp / "plan.jpg"
        source.write_bytes(_photo(1200, 900))
        first = ensure_thumbnail(source, self._target("plan.jpg"))
        # Destroy the source: a cache hit must not re-read or regenerate it.
        source.write_bytes(b"this is not an image")
        second = ensure_thumbnail(source, self._target("plan.jpg"))
        self.assertEqual(first, second)

    def test_undecodable_source_returns_none_and_writes_nothing(self):
        source = self.tmp / "plan.jpg"
        source.write_bytes(b"\xff\xd8\xff broken payload")
        target = self._target("plan.jpg")
        self.assertIsNone(ensure_thumbnail(source, target))
        self.assertFalse(target.exists())

    def test_exif_orientation_is_applied(self):
        """A portrait photo tagged orientation=6 must not come out rotated.

        Browsers honour EXIF when rendering the full image, so a thumbnail
        that skipped it would appear turned relative to the zoomed view.
        """
        source = self.tmp / "plan.jpg"
        source.write_bytes(_photo(400, 900))
        with Image.open(source) as original:
            exif = original.getexif()
            exif[274] = 6  # Orientation: rotate 90 degrees
            buffer = io.BytesIO()
            original.save(buffer, "JPEG", exif=exif)
        source.write_bytes(buffer.getvalue())
        data = ensure_thumbnail(source, self._target("plan.jpg"))
        self.assertIsNotNone(data)
        with Image.open(io.BytesIO(data)) as thumb:
            self.assertGreater(thumb.size[0], thumb.size[1], "应用 EXIF 后缩略图应为横向")

    def test_non_rgb_modes_are_converted_to_jpeg(self):
        source = self.tmp / "plan.png"
        buffer = io.BytesIO()
        Image.effect_noise((600, 400), 40).convert("RGBA").save(buffer, "PNG")
        source.write_bytes(buffer.getvalue())
        data = ensure_thumbnail(source, self._target("plan.png"))
        self.assertIsNotNone(data)
        with Image.open(io.BytesIO(data)) as thumb:
            self.assertEqual(thumb.format, "JPEG")


if __name__ == "__main__":
    unittest.main()
