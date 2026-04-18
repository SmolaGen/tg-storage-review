import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.files import router as files_router
from app.api.folders import router as folders_router
from app.api.health import router as health_router
from app.api.storages import router as storages_router
from app.bot import create_bot_client
from app.config import settings  # noqa: F401
from app.database import async_session, engine
from app.jobs.janitor import run_janitor_loop
from app.limiter import limiter
from app.models.storage_worker import StorageWorker
from app.state import TelegramPool

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = TelegramPool.get_instance()
    logger.info("Starting up: TelegramPool initialized")

    async def _connect_telegram() -> None:
        """Load workers and bot in background so HTTP starts immediately."""
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(StorageWorker).where(
                        StorageWorker.session_string.isnot(None)
                    )
                )
                workers = result.scalars().all()
                loaded = 0
                for w in workers:
                    try:
                        await pool.get_or_create(
                            worker_id=w.id,
                            bot_token=w.bot_token,
                            session_string=w.session_string,
                            api_id=settings.tg_api_id,
                            api_hash=settings.tg_api_hash,
                        )
                        loaded += 1
                    except Exception:
                        logger.exception("Failed to load worker %s into pool", w.id)
                logger.info(
                    "TelegramPool: loaded %d/%d workers from DB",
                    loaded,
                    len(workers),
                )
        except Exception:
            logger.exception("TelegramPool: failed to load workers from DB (non-fatal)")

        try:
            bot_client = await create_bot_client()
            if bot_client is not None:
                app.state.bot_client = bot_client
                logger.info("Telegram bot started")
            else:
                logger.info("Telegram bot skipped (no tg_bot_token)")
        except Exception:
            logger.exception("Telegram bot failed to start (non-fatal)")

    # Telegram connections in background — server starts immediately
    telegram_task = asyncio.create_task(_connect_telegram())
    app.state.telegram_task = telegram_task

    # Start janitor background job
    janitor_task = asyncio.create_task(run_janitor_loop())
    logger.info("Janitor background job started")

    yield

    # Shutdown: cancel background tasks, disconnect pool and DB
    janitor_task.cancel()
    telegram_task.cancel()
    try:
        await janitor_task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("janitor_task raised during shutdown")
    try:
        await telegram_task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("telegram_task raised during shutdown")
    logger.info("Background tasks stopped")

    bot_client = getattr(app.state, "bot_client", None)
    if bot_client is not None:
        try:
            await bot_client.disconnect()
            logger.info("Telegram bot disconnected")
        except Exception:
            logger.exception("Error disconnecting Telegram bot")

    await pool.shutdown()
    logger.info("TelegramPool shut down")
    await engine.dispose()
    logger.info("Shutdown: DB engine disposed")


app = FastAPI(title="tg-storage", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(health_router)
app.include_router(admin_router)
app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(storages_router, prefix="/storages", tags=["storages"])
app.include_router(folders_router, prefix="/folders", tags=["folders"])
app.include_router(files_router, prefix="/files", tags=["files"])

# Serve static UI
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def ui() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))
