import logging

from fastapi import APIRouter
from sqlalchemy import text

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/health")
async def health_check():
    """Public health endpoint reporting DB and TelegramPool worker status."""
    from app.database import async_session
    from app.state import TelegramPool

    # DB check
    db_status = "ok"
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("health_check: DB probe failed")
        db_status = "error"

    # Pool check
    pool = TelegramPool.get_instance()
    workers = pool.health()

    # Overall status
    all_connected = all(w["connected"] for w in workers) if workers else True
    overall = "ok" if db_status == "ok" and all_connected else "degraded"

    return {
        "status": overall,
        "db": db_status,
        "telegram_pool": {
            "active_workers": len(workers),
            "workers": workers,
        },
    }
