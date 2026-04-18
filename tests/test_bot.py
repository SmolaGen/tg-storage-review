"""Tests for app.bot — Telegram bot handlers.

Strategy: patch TelegramClient so create_bot_client() registers handlers
on a real (but non-connected) client, then extract and invoke the
handler callbacks directly with mock events.  async_session is patched
to use the test DB session factory from conftest.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file import File
from app.models.storage import Storage
from app.models.storage_worker import StorageWorker
from app.models.user import User
from app.services.file import MAX_FILE_SIZE
from tests.conftest import test_session_factory


# ── Helpers ──────────────────────────────────────────────────────


def _make_mock_event(
    sender_id: int = 123456789,
    has_document: bool = True,
    file_size: int = 1024,
    file_name: str = "test.txt",
    mime_type: str = "text/plain",
    text: str | None = None,
    has_media: bool | None = None,
) -> MagicMock:
    """Build a mock Telethon NewMessage event.

    Returns a MagicMock that satisfies the attribute accesses in
    handle_start, handle_file, and handle_text.
    """
    event = AsyncMock()
    event.sender_id = sender_id

    # message.text
    event.message.text = text

    if has_document:
        event.message.document = MagicMock()
        if has_media is None:
            event.message.media = MagicMock()
        else:
            event.message.media = MagicMock() if has_media else None

        # file attrs
        file_mock = MagicMock()
        file_mock.size = file_size
        file_mock.name = file_name
        file_mock.mime_type = mime_type
        event.message.file = file_mock
    else:
        event.message.document = None
        if has_media is None:
            event.message.media = None
        else:
            event.message.media = MagicMock() if has_media else None
        event.message.file = None

    # reply is async
    event.reply = AsyncMock()

    # client.download_media is async
    event.client = AsyncMock()
    event.client.download_media = AsyncMock(return_value=b"file-content-bytes")

    return event


async def _seed_user(session: AsyncSession, user_id: int = 123456789) -> User:
    user = User(id=user_id, first_name="Test", username="testuser")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _seed_storage(
    session: AsyncSession, user_id: int = 123456789
) -> Storage:
    storage = Storage(user_id=user_id, name="My Storage", chat_id=-1001234567890)
    session.add(storage)
    await session.commit()
    await session.refresh(storage)
    return storage


async def _seed_worker(session: AsyncSession, storage_id: uuid.UUID) -> StorageWorker:
    worker = StorageWorker(
        storage_id=storage_id,
        bot_token="fake-bot-token:AAAAAA",
        session_string=None,
    )
    session.add(worker)
    await session.commit()
    await session.refresh(worker)
    return worker


# ── Fixture: extract handlers from create_bot_client ─────────────


@pytest_asyncio.fixture
async def bot_handlers():
    """Patch TelegramClient.start and create_bot_client, returning
    a dict mapping handler names to their callbacks.

    This avoids any real network calls — we only care about the
    @client.on() decorated functions that get registered.
    """
    with (
        patch("app.bot.TelegramClient") as MockClientClass,
        patch("app.bot.settings") as mock_settings,
    ):
        # Build a real-ish mock client that records event handlers
        mock_client = AsyncMock()
        registered_handlers: list[tuple] = []

        def mock_on(event_builder):
            """Mimic @client.on(event_builder)"""
            def decorator(func):
                registered_handlers.append((func, event_builder))
                return func
            return decorator

        mock_client.on = mock_on
        mock_client.start = AsyncMock()
        MockClientClass.return_value = mock_client

        mock_settings.tg_bot_token = "test-token"
        mock_settings.tg_api_id = 12345
        mock_settings.tg_api_hash = "testhash"

        from app.bot import create_bot_client

        result = await create_bot_client()
        assert result is mock_client

        # Map handler names
        handlers = {}
        for func, builder in registered_handlers:
            handlers[func.__name__] = func

        assert "handle_start" in handlers
        assert "handle_file" in handlers
        assert "handle_text" in handlers

        yield handlers


# ── Tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_command(bot_handlers):
    """The /start handler replies with a welcome message."""
    event = _make_mock_event(text="/start", has_document=False)
    await bot_handlers["handle_start"](event)

    event.reply.assert_called_once()
    reply_text = event.reply.call_args[0][0]
    assert "Welcome" in reply_text or "welcome" in reply_text.lower()


@pytest.mark.asyncio
async def test_user_not_found(bot_handlers, db_session):
    """Unregistered sender gets a registration error."""
    event = _make_mock_event(sender_id=999999)

    with patch("app.bot.async_session", test_session_factory):
        await bot_handlers["handle_file"](event)

    event.reply.assert_called_once()
    reply_text = event.reply.call_args[0][0]
    assert "not registered" in reply_text.lower() or "register" in reply_text.lower()


@pytest.mark.asyncio
async def test_no_storage(bot_handlers, db_session):
    """User without storage gets storage guidance."""
    await _seed_user(db_session, user_id=111111)
    event = _make_mock_event(sender_id=111111)

    with patch("app.bot.async_session", test_session_factory):
        await bot_handlers["handle_file"](event)

    event.reply.assert_called_once()
    reply_text = event.reply.call_args[0][0]
    assert "no storage" in reply_text.lower() or "storage" in reply_text.lower()


@pytest.mark.asyncio
async def test_no_workers(bot_handlers, db_session):
    """Storage without workers gets worker guidance."""
    await _seed_user(db_session, user_id=222222)
    storage = await _seed_storage(db_session, user_id=222222)
    # No worker seeded
    event = _make_mock_event(sender_id=222222)

    with patch("app.bot.async_session", test_session_factory):
        await bot_handlers["handle_file"](event)

    event.reply.assert_called_once()
    reply_text = event.reply.call_args[0][0]
    assert "worker" in reply_text.lower() or "no upload" in reply_text.lower()


@pytest.mark.asyncio
async def test_file_too_large(bot_handlers, db_session):
    """File exceeding MAX_FILE_SIZE gets size limit message."""
    await _seed_user(db_session, user_id=333333)
    storage = await _seed_storage(db_session, user_id=333333)
    await _seed_worker(db_session, storage.id)

    event = _make_mock_event(sender_id=333333, file_size=MAX_FILE_SIZE + 1)

    with patch("app.bot.async_session", test_session_factory):
        await bot_handlers["handle_file"](event)

    event.reply.assert_called_once()
    reply_text = event.reply.call_args[0][0]
    assert "too large" in reply_text.lower() or "maximum" in reply_text.lower()


@pytest.mark.asyncio
async def test_successful_file_save(bot_handlers, db_session):
    """Full happy path: file downloaded, ingested, success reply."""
    await _seed_user(db_session, user_id=444444)
    storage = await _seed_storage(db_session, user_id=444444)
    await _seed_worker(db_session, storage.id)

    file_bytes = b"hello-world-content"
    event = _make_mock_event(
        sender_id=444444,
        file_size=len(file_bytes),
        file_name="hello.txt",
        mime_type="text/plain",
    )
    event.client.download_media = AsyncMock(return_value=file_bytes)

    with (
        patch("app.bot.async_session", test_session_factory),
        patch("app.bot.ingest_file", new_callable=AsyncMock) as mock_ingest,
    ):
        await bot_handlers["handle_file"](event)

    # Verify success reply
    event.reply.assert_called_once()
    reply_text = event.reply.call_args[0][0]
    assert "hello.txt" in reply_text
    assert "✅" in reply_text or "saved" in reply_text.lower()

    # Verify ingest_file was called with correct args
    mock_ingest.assert_called_once()
    call_args = mock_ingest.call_args
    assert call_args[0][1] == storage.id  # storage_id
    assert call_args[0][2] == file_bytes  # data


@pytest.mark.asyncio
async def test_download_failure(bot_handlers, db_session):
    """download_media raises → error reply."""
    await _seed_user(db_session, user_id=555555)
    storage = await _seed_storage(db_session, user_id=555555)
    await _seed_worker(db_session, storage.id)

    event = _make_mock_event(sender_id=555555)
    event.client.download_media = AsyncMock(
        side_effect=RuntimeError("Telegram download error")
    )

    with patch("app.bot.async_session", test_session_factory):
        await bot_handlers["handle_file"](event)

    event.reply.assert_called_once()
    reply_text = event.reply.call_args[0][0]
    assert "download" in reply_text.lower() or "failed" in reply_text.lower()


@pytest.mark.asyncio
async def test_download_returns_none(bot_handlers, db_session):
    """download_media returns None → error reply."""
    await _seed_user(db_session, user_id=666666)
    storage = await _seed_storage(db_session, user_id=666666)
    await _seed_worker(db_session, storage.id)

    event = _make_mock_event(sender_id=666666)
    event.client.download_media = AsyncMock(return_value=None)

    with patch("app.bot.async_session", test_session_factory):
        await bot_handlers["handle_file"](event)

    event.reply.assert_called_once()
    reply_text = event.reply.call_args[0][0]
    assert "download" in reply_text.lower() or "failed" in reply_text.lower()


@pytest.mark.asyncio
async def test_ingest_failure(bot_handlers, db_session):
    """ingest_file raises RuntimeError → error reply."""
    await _seed_user(db_session, user_id=777777)
    storage = await _seed_storage(db_session, user_id=777777)
    await _seed_worker(db_session, storage.id)

    file_bytes = b"some data"
    event = _make_mock_event(sender_id=777777, file_size=len(file_bytes))
    event.client.download_media = AsyncMock(return_value=file_bytes)

    with (
        patch("app.bot.async_session", test_session_factory),
        patch(
            "app.bot.ingest_file",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Chunk upload failed"),
        ),
    ):
        await bot_handlers["handle_file"](event)

    event.reply.assert_called_once()
    reply_text = event.reply.call_args[0][0]
    assert "failed" in reply_text.lower() or "error" in reply_text.lower()


@pytest.mark.asyncio
async def test_text_message_no_file(bot_handlers):
    """Text-only message (no media) gets fallback reply."""
    event = _make_mock_event(text="Hello", has_document=False)
    await bot_handlers["handle_text"](event)

    event.reply.assert_called_once()
    reply_text = event.reply.call_args[0][0]
    assert "file" in reply_text.lower() or "send" in reply_text.lower()


@pytest.mark.asyncio
async def test_zero_size_file(bot_handlers, db_session):
    """Edge case: file.size = 0 — should still proceed (empty file)."""
    await _seed_user(db_session, user_id=888888)
    storage = await _seed_storage(db_session, user_id=888888)
    await _seed_worker(db_session, storage.id)

    event = _make_mock_event(sender_id=888888, file_size=0, file_name="empty.txt")
    event.client.download_media = AsyncMock(return_value=b"")

    with (
        patch("app.bot.async_session", test_session_factory),
        patch("app.bot.ingest_file", new_callable=AsyncMock) as mock_ingest,
    ):
        await bot_handlers["handle_file"](event)

    # Should succeed (empty file edge case)
    event.reply.assert_called_once()
    reply_text = event.reply.call_args[0][0]
    assert "empty.txt" in reply_text
    assert "✅" in reply_text or "saved" in reply_text.lower()
    mock_ingest.assert_called_once()


@pytest.mark.asyncio
async def test_bot_disabled_returns_none():
    """create_bot_client returns None when tg_bot_token is empty."""
    with patch("app.bot.settings") as mock_settings:
        mock_settings.tg_bot_token = ""
        from app.bot import create_bot_client

        result = await create_bot_client()
        assert result is None
