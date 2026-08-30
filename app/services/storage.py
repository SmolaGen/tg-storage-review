"""Storage service: Telegram bot validation, worker registration, and CRUD operations.

validate_and_register_worker encapsulates all Telethon logic for:
1. Connecting a bot by token
2. Verifying the bot is not banned/deactivated
3. Verifying the bot has access to the target channel
4. Extracting the StringSession for persistence

StorageService provides CRUD functions for storages and workers.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient
from telethon.errors import (
    AccessTokenExpiredError,
    AccessTokenInvalidError,
    AuthKeyUnregisteredError,
    ChannelInvalidError,
    ChannelPrivateError,
    ChatForbiddenError,
    ChatWriteForbiddenError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.sessions import StringSession

from app.config import settings
from app.models.storage import Storage
from app.models.storage_worker import StorageWorker

logger = logging.getLogger(__name__)


# ── Telegram validation ─────────────────────────────────────────


async def validate_and_register_worker(
    bot_token: str,
    chat_id: int,
) -> str:
    """Validate a bot_token against a Telegram channel and return a session_string.

    Steps:
    1. Create a temporary TelegramClient with a fresh StringSession.
    2. Start the client with the given bot_token.
    3. Verify the bot can access the channel (chat_id).
    4. Export and return the StringSession for persistence.

    Raises:
        ValueError: with one of three messages:
            - "Invalid or expired bot_token"
            - "Bot account is banned"
            - "Bot does not have access to the channel"
    """
    session = StringSession()
    client = TelegramClient(session, settings.tg_api_id, settings.tg_api_hash)

    try:
        # Step 1: Connect and authenticate with bot_token
        try:
            await client.start(bot_token=bot_token)
        except (
            AccessTokenInvalidError,
            AccessTokenExpiredError,
            AuthKeyUnregisteredError,
        ) as exc:
            logger.warning("Invalid bot_token: %s", exc)
            raise ValueError("Invalid or expired bot_token") from exc
        except (UserDeactivatedError, UserDeactivatedBanError) as exc:
            logger.warning("Banned bot: %s", exc)
            raise ValueError("Bot account is banned") from exc
        except Exception as exc:
            # Catch-all for unexpected auth errors (RPCError subtypes, network, etc.)
            error_msg = str(exc).lower()
            if "access_token" in error_msg or "bot_token" in error_msg:
                logger.warning("Bot token error: %s", exc)
                raise ValueError("Invalid or expired bot_token") from exc
            if "deactivated" in error_msg or "banned" in error_msg:
                logger.warning("Banned bot (generic): %s", exc)
                raise ValueError("Bot account is banned") from exc
            logger.error("Unexpected error during bot auth: %s", exc)
            raise ValueError("Invalid or expired bot_token") from exc

        # Step 2: Verify bot has access to the channel
        try:
            entity = await client.get_entity(chat_id)
            logger.info(
                "Bot validated for channel %s (entity: %s)",
                chat_id,
                getattr(entity, "title", chat_id),
            )
        except (
            ChannelPrivateError,
            ChannelInvalidError,
            ChatForbiddenError,
            ChatWriteForbiddenError,
        ) as exc:
            logger.warning("Bot has no access to channel %s: %s", chat_id, exc)
            raise ValueError("Bot does not have access to the channel") from exc
        except ValueError as exc:
            # Telethon raises plain ValueError for unresolvable entities
            logger.warning("Cannot resolve channel %s: %s", chat_id, exc)
            raise ValueError("Bot does not have access to the channel") from exc
        except Exception as exc:
            error_msg = str(exc).lower()
            if any(
                k in error_msg for k in ("private", "forbidden", "not found", "invalid")
            ):
                logger.warning("Channel access denied %s: %s", chat_id, exc)
                raise ValueError("Bot does not have access to the channel") from exc
            logger.error("Unexpected error checking channel %s: %s", chat_id, exc)
            raise ValueError("Bot does not have access to the channel") from exc

        # Step 3: Export session string
        session_string = client.session.save()
        logger.info("Session string exported for bot (chat_id=%s)", chat_id)
        return session_string

    finally:
        await client.disconnect()


# ── Storage CRUD ─────────────────────────────────────────────────


async def create_storage(
    session: AsyncSession,
    user_id: int,
    name: str,
    chat_id: int,
) -> Storage:
    """Create a new storage for the given user."""
    storage = Storage(user_id=user_id, name=name, chat_id=chat_id)
    session.add(storage)
    await session.commit()
    await session.refresh(storage)
    logger.info("Created storage %s for user %s", storage.id, user_id)
    return storage


async def list_storages(
    session: AsyncSession,
    user_id: int,
) -> list[dict]:
    """List all storages for a user, annotated with worker_count.

    Returns list of dicts with storage fields + worker_count.
    """
    worker_count_subq = (
        select(
            StorageWorker.storage_id,
            func.count(StorageWorker.id).label("worker_count"),
        )
        .group_by(StorageWorker.storage_id)
        .subquery()
    )

    stmt = (
        select(
            Storage,
            func.coalesce(worker_count_subq.c.worker_count, 0).label("worker_count"),
        )
        .outerjoin(worker_count_subq, Storage.id == worker_count_subq.c.storage_id)
        .where(Storage.user_id == user_id)
        .order_by(Storage.created_at.desc())
    )

    result = await session.execute(stmt)
    rows = result.all()

    return [
        {
            "id": row.Storage.id,
            "name": row.Storage.name,
            "chat_id": row.Storage.chat_id,
            "created_at": row.Storage.created_at,
            "worker_count": row.worker_count,
        }
        for row in rows
    ]


async def get_storage_by_id(
    session: AsyncSession,
    storage_id: UUID,
) -> Storage | None:
    """Fetch a storage by ID, or None."""
    return await session.get(Storage, storage_id)


async def add_worker(
    session: AsyncSession,
    storage_id: UUID,
    bot_token: str,
    session_string: str,
) -> StorageWorker:
    """Persist a new worker to a storage."""
    worker = StorageWorker(
        storage_id=storage_id,
        bot_token=bot_token,
        session_string=session_string,
    )
    session.add(worker)
    await session.commit()
    await session.refresh(worker)
    logger.info("Added worker %s to storage %s", worker.id, storage_id)
    return worker


async def list_workers(
    session: AsyncSession,
    storage_id: UUID,
) -> list[StorageWorker]:
    """List all workers for a given storage."""
    stmt = (
        select(StorageWorker)
        .where(StorageWorker.storage_id == storage_id)
        .order_by(StorageWorker.created_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
