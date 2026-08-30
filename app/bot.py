"""Telegram bot interface for file ingestion.

Provides ``create_bot_client()`` which builds a Telethon ``TelegramClient``
running as a bot. Users can forward or send files directly to the bot;
files are saved into the user's first storage via :func:`ingest_file`.

If ``settings.tg_bot_token`` is empty, the bot is disabled and
``create_bot_client()`` returns ``None``.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from app.config import settings
from app.database import async_session
from app.models.file import File
from app.models.storage import Storage
from app.models.storage_worker import StorageWorker
from app.models.user import User
from app.services.file import MAX_FILE_SIZE, ingest_file

logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────────────


def _human_size(size: int) -> str:
    """Return a human-readable file size string."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f} TB"


# ── Bot factory ──────────────────────────────────────────────────


async def create_bot_client() -> TelegramClient | None:
    """Create, configure, and start the Telegram bot client.

    Returns ``None`` if ``settings.tg_bot_token`` is empty (bot disabled).
    The caller is responsible for calling ``await client.disconnect()``
    during shutdown.
    """
    if not settings.tg_bot_token:
        logger.info("bot_skip: tg_bot_token is empty, bot disabled")
        return None

    client = TelegramClient(
        StringSession(),
        settings.tg_api_id,
        settings.tg_api_hash,
    )

    # Override DC port if non-standard (e.g. port 80 when 443 blocked)
    if settings.tg_dc_port != 443:
        from app.state import _DC_IPS

        dc_id = client.session.dc_id or 2
        addr = _DC_IPS.get(dc_id, "149.154.167.51")
        client.session.set_dc(dc_id, addr, settings.tg_dc_port)

    # ── /start handler ───────────────────────────────────────────

    @client.on(events.NewMessage(pattern="/start"))
    async def handle_start(event: events.NewMessage.Event) -> None:
        """Welcome message explaining how to use the bot."""
        await event.reply(
            "👋 Welcome to tg-storage bot!\n\n"
            "Send or forward any file to me and I'll save it to your storage.\n\n"
            "Your Telegram account must be registered in the system."
        )

    # ── File handler ─────────────────────────────────────────────

    @client.on(events.NewMessage(func=lambda e: e.message.document or e.message.media))
    async def handle_file(event: events.NewMessage.Event) -> None:
        """Receive a file, validate, ingest, and reply with confirmation."""
        sender_id = event.sender_id

        try:
            # 1. Look up user
            async with async_session() as session:
                user = await session.get(User, sender_id)
            if user is None:
                await event.reply("❌ You are not registered in the system.")
                return

            # 2. Check file exists and has size
            tg_file = event.message.file
            if tg_file is None or tg_file.size is None:
                await event.reply("⚠️ Please send a file.")
                return

            file_size: int = tg_file.size
            file_name: str = tg_file.name or "unnamed"
            mime_type: str | None = tg_file.mime_type

            # 3. Size check
            if file_size > MAX_FILE_SIZE:
                await event.reply(
                    f"❌ File too large ({_human_size(file_size)}). "
                    f"Maximum size is {_human_size(MAX_FILE_SIZE)}."
                )
                return

            # 4. Auto-select first storage
            async with async_session() as session:
                result = await session.execute(
                    select(Storage).where(Storage.user_id == sender_id).limit(1)
                )
                storage = result.scalar_one_or_none()
            if storage is None:
                await event.reply("❌ You have no storage configured.")
                return

            storage_id: UUID = storage.id

            # 5. Check storage has workers
            async with async_session() as session:
                result = await session.execute(
                    select(StorageWorker)
                    .where(StorageWorker.storage_id == storage_id)
                    .limit(1)
                )
                worker = result.scalar_one_or_none()
            if worker is None:
                await event.reply("❌ Your storage has no upload workers configured.")
                return

            # 6. Download file from Telegram
            try:
                data: bytes = await event.client.download_media(
                    event.message, file=bytes
                )  # type: ignore[assignment]
            except Exception as download_exc:
                logger.error(
                    "bot_download_failed sender_id=%d error=%s",
                    sender_id,
                    download_exc,
                )
                await event.reply("❌ Failed to download file from Telegram.")
                return

            if data is None:
                await event.reply("❌ Failed to download file from Telegram.")
                return

            # 7. Create File record
            async with async_session() as session:
                file_record = File(
                    storage_id=storage_id,
                    name=file_name,
                    size=len(data),
                    mime_type=mime_type,
                    is_uploaded=False,
                )
                session.add(file_record)
                await session.commit()
                await session.refresh(file_record)
                file_id = file_record.id

            # 8. Ingest (chunk + upload)
            try:
                await ingest_file(file_id, storage_id, data)
            except Exception as ingest_exc:
                logger.error(
                    "bot_ingest_failed file_id=%s sender_id=%d error=%s",
                    file_id,
                    sender_id,
                    ingest_exc,
                )
                await event.reply("❌ Upload failed. Please try again later.")
                return

            # 9. Success reply
            await event.reply(
                f"✅ File saved: {file_name} ({_human_size(len(data))})\nID: {file_id}"
            )
            logger.info(
                "bot_file_saved file_id=%s sender_id=%d name=%s size=%d",
                file_id,
                sender_id,
                file_name,
                len(data),
            )

        except Exception as exc:
            logger.exception("bot_handler_error sender_id=%d error=%s", sender_id, exc)
            await event.reply("❌ An unexpected error occurred.")

    # ── Fallback: text-only messages (no file) ───────────────────

    @client.on(events.NewMessage)
    async def handle_text(event: events.NewMessage.Event) -> None:
        """Reply to text-only messages that aren't commands."""
        # Skip if message has media/document (handled above) or is /start
        if event.message.document or event.message.media:
            return
        if event.message.text and event.message.text.startswith("/start"):
            return

        await event.reply(
            "📎 Please send or forward a file to save it to your storage."
        )

    # ── Start the client ─────────────────────────────────────────

    await client.start(bot_token=settings.tg_bot_token)
    logger.info("bot_started")
    return client
