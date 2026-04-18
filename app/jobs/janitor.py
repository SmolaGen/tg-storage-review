import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.config import settings

logger = logging.getLogger(__name__)


async def cleanup_stale_files() -> int:
    """Single-pass cleanup of zombie file records.

    Finds files where is_uploaded=False AND is_deleted=False AND created_at
    is older than janitor_stale_minutes. Soft-deletes them by setting
    is_deleted=True. Returns count of cleaned files.
    """
    from app.database import async_session
    from app.models.file import File

    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=settings.janitor_stale_minutes
    )

    async with async_session() as session:
        result = await session.execute(
            update(File)
            .where(
                File.is_uploaded == False,  # noqa: E712
                File.is_deleted == False,  # noqa: E712
                File.created_at < cutoff,
            )
            .values(is_deleted=True)
            .returning(File.id)
        )
        cleaned = len(result.all())
        await session.commit()

    logger.info("janitor_run files_cleaned=%d", cleaned)
    return cleaned


async def run_janitor_loop() -> None:
    """Infinite loop that periodically runs cleanup_stale_files.

    Never crashes — catches all exceptions inside the loop.
    """
    while True:
        try:
            await cleanup_stale_files()
        except Exception:
            logger.exception("janitor_error")
        await asyncio.sleep(settings.janitor_interval_seconds)
