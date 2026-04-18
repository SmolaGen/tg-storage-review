"""Thumbnail service: disk-cached WebP generation for images and videos.

Provides:
- get_or_generate_thumbnail      — image or video poster (disk-cached)
- get_or_generate_video_preview  — animated WebP preview (disk-cached)
- pre_generate_from_data         — pre-warm cache during upload
- compute_blurhash               — blurhash string for instant placeholders
- evict_thumbnail_cache          — remove cached files on soft-delete
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import subprocess
import tempfile
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

ALLOWED_SIZES: frozenset[int] = frozenset({200, 400, 800})
DEFAULT_QUALITY = 60
THUMB_CACHE_DIR = Path("/tmp/tg-thumbnails")


# ── Cache path helpers ────────────────────────────────────────────


def _cache_path(file_id: str, suffix: str) -> Path:
    """Two-level sharded directory to avoid >1000 files in one dir."""
    h = hashlib.sha256(f"{file_id}:{suffix}".encode()).hexdigest()
    return THUMB_CACHE_DIR / h[:2] / h[2:4] / f"{h}.webp"


def _read_cache(path: Path) -> bytes | None:
    try:
        return path.read_bytes() if path.exists() else None
    except OSError:
        return None


def _write_cache(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# ── Image processing ──────────────────────────────────────────────


def _image_thumbnail_sync(data: bytes, size: int, quality: int) -> bytes | None:
    """Generate WebP thumbnail from image bytes (sync, CPU-bound via Pillow)."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        img.thumbnail((size, size), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="WEBP", quality=quality, method=6)
        return out.getvalue()
    except Exception as exc:
        logger.warning("image_thumbnail_failed: %s", exc)
        return None


def compute_blurhash(data: bytes) -> str | None:
    """Compute blurhash from image bytes. Returns None on failure."""
    try:
        import blurhash
        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("RGB")
        small = img.copy()
        small.thumbnail((64, 64))
        buf = io.BytesIO()
        small.save(buf, format="PNG")
        buf.seek(0)
        return blurhash.encode(buf, x_components=4, y_components=3)
    except Exception as exc:
        logger.warning("blurhash_failed: %s", exc)
        return None


# ── Video processing (ffmpeg subprocess) ─────────────────────────


def _video_poster_sync(data: bytes, size: int) -> bytes | None:
    """Extract first frame from video as WebP (blocking subprocess)."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fin:
            fin.write(data)
            input_path = Path(fin.name)

        out_path = input_path.with_suffix(".webp")
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    "1",
                    "-i",
                    str(input_path),
                    "-vf",
                    f"scale={size}:-2",
                    "-vframes",
                    "1",
                    "-c:v",
                    "libwebp",
                    str(out_path),
                ],
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0 or not out_path.exists():
                logger.warning("ffmpeg_poster_failed stderr=%s", result.stderr[:300])
                return None
            return out_path.read_bytes()
        finally:
            input_path.unlink(missing_ok=True)
            out_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("video_poster_failed: %s", exc)
        return None


def _video_animated_preview_sync(data: bytes) -> bytes | None:
    """Generate animated WebP: first 5 sec, 3 fps, 320px wide (blocking subprocess)."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fin:
            fin.write(data)
            input_path = Path(fin.name)

        out_path = input_path.with_suffix(".webp")
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(input_path),
                    "-t",
                    "5",
                    "-vf",
                    "scale=320:-2,fps=3",
                    "-loop",
                    "0",
                    "-c:v",
                    "libwebp",
                    "-lossless",
                    "0",
                    "-quality",
                    "70",
                    str(out_path),
                ],
                capture_output=True,
                timeout=60,
            )
            if result.returncode != 0 or not out_path.exists():
                logger.warning("ffmpeg_preview_failed stderr=%s", result.stderr[:300])
                return None
            return out_path.read_bytes()
        finally:
            input_path.unlink(missing_ok=True)
            out_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("video_animated_preview_failed: %s", exc)
        return None


# ── Public async API ──────────────────────────────────────────────


async def get_or_generate_thumbnail(
    session: AsyncSession,
    file_id: UUID,
    user_id: int,
    size: int,
    quality: int,
    mime: str,
) -> bytes | None:
    """Return disk-cached thumbnail or generate it.

    Cache hit → disk read only. Cache miss → Telegram download + Pillow/ffmpeg.
    Returns None if file type is unsupported or generation fails.
    """
    from app.services.file import download_file_bytes

    path = _cache_path(str(file_id), f"thumb-{size}-{quality}")
    cached = _read_cache(path)
    if cached is not None:
        logger.debug("thumbnail_cache_hit file_id=%s size=%d", file_id, size)
        return cached

    logger.info("thumbnail_cache_miss file_id=%s size=%d", file_id, size)
    data = await download_file_bytes(session, file_id, user_id)

    if mime.startswith("image/"):
        thumb = await asyncio.to_thread(_image_thumbnail_sync, data, size, quality)
    elif mime.startswith("video/"):
        thumb = await asyncio.to_thread(_video_poster_sync, data, size)
    else:
        return None

    if thumb is not None:
        _write_cache(path, thumb)

    return thumb


async def get_or_generate_video_preview(
    session: AsyncSession,
    file_id: UUID,
    user_id: int,
) -> bytes | None:
    """Return disk-cached animated WebP preview or generate it via ffmpeg."""
    from app.services.file import download_file_bytes

    path = _cache_path(str(file_id), "preview")
    cached = _read_cache(path)
    if cached is not None:
        logger.debug("preview_cache_hit file_id=%s", file_id)
        return cached

    logger.info("preview_cache_miss file_id=%s", file_id)
    data = await download_file_bytes(session, file_id, user_id)
    preview = await asyncio.to_thread(_video_animated_preview_sync, data)

    if preview is not None:
        _write_cache(path, preview)

    return preview


def pre_generate_from_data(
    file_id: UUID, mime_type: str | None, data: bytes
) -> str | None:
    """Pre-warm thumbnail cache from raw upload bytes. Returns blurhash or None.

    Called synchronously in a background thread after upload completes.
    Images: generates all 3 preset sizes + computes blurhash.
    Videos: generates poster at 200 and 400px (animated preview is on-demand).
    """
    if not mime_type:
        return None

    blurhash_str: str | None = None

    if mime_type.startswith("image/"):
        blurhash_str = compute_blurhash(data)
        for size in sorted(ALLOWED_SIZES):
            path = _cache_path(str(file_id), f"thumb-{size}-{DEFAULT_QUALITY}")
            if not _read_cache(path):
                thumb = _image_thumbnail_sync(data, size, DEFAULT_QUALITY)
                if thumb is not None:
                    _write_cache(path, thumb)
                    logger.info("pre_generated_thumb file_id=%s size=%d", file_id, size)

    elif mime_type.startswith("video/"):
        for size in (200, 400):
            path = _cache_path(str(file_id), f"thumb-{size}-{DEFAULT_QUALITY}")
            if not _read_cache(path):
                thumb = _video_poster_sync(data, size)
                if thumb is not None:
                    _write_cache(path, thumb)
                    logger.info(
                        "pre_generated_video_thumb file_id=%s size=%d", file_id, size
                    )

    return blurhash_str


def evict_thumbnail_cache(file_id: UUID) -> None:
    """Remove all disk-cached thumbnails and previews for a file."""
    file_id_str = str(file_id)
    suffixes = [f"thumb-{size}-{DEFAULT_QUALITY}" for size in ALLOWED_SIZES]
    suffixes.append("preview")

    removed = 0
    for suffix in suffixes:
        path = _cache_path(file_id_str, suffix)
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass

    logger.info("evicted_cache file_id=%s entries=%d", file_id, removed)
