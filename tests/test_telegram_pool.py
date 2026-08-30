"""Tests for TelegramPool singleton and validate_and_register_worker.

All Telethon I/O is mocked — no real Telegram connections.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.state import TelegramPool


# ── TelegramPool tests ──────────────────────────────────────────


class TestTelegramPoolSingleton:
    """TelegramPool singleton lifecycle and management."""

    def setup_method(self) -> None:
        TelegramPool.reset()

    def teardown_method(self) -> None:
        TelegramPool.reset()

    def test_singleton_returns_same_instance(self) -> None:
        pool1 = TelegramPool.get_instance()
        pool2 = TelegramPool.get_instance()
        assert pool1 is pool2

    def test_reset_creates_new_instance(self) -> None:
        pool1 = TelegramPool.get_instance()
        TelegramPool.reset()
        pool2 = TelegramPool.get_instance()
        assert pool1 is not pool2

    def test_initial_state_empty(self) -> None:
        pool = TelegramPool.get_instance()
        assert pool.active_count == 0

    @pytest.mark.asyncio
    async def test_get_or_create_new_client(self) -> None:
        pool = TelegramPool()
        worker_id = uuid4()
        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        with patch("app.state.TelegramClient", return_value=mock_client):
            client = await pool.get_or_create(
                worker_id=worker_id,
                bot_token="fake:token",
                session_string=None,
                api_id=12345,
                api_hash="fakehash",
            )

        assert client is mock_client
        mock_client.start.assert_awaited_once_with(bot_token="fake:token")
        assert pool.active_count == 1

    @pytest.mark.asyncio
    async def test_get_or_create_returns_existing(self) -> None:
        pool = TelegramPool()
        worker_id = uuid4()
        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        with patch("app.state.TelegramClient", return_value=mock_client):
            c1 = await pool.get_or_create(
                worker_id=worker_id,
                bot_token="fake:token",
                session_string=None,
                api_id=12345,
                api_hash="fakehash",
            )
            c2 = await pool.get_or_create(
                worker_id=worker_id,
                bot_token="fake:token",
                session_string=None,
                api_id=12345,
                api_hash="fakehash",
            )

        assert c1 is c2
        assert pool.active_count == 1

    @pytest.mark.asyncio
    async def test_get_or_create_with_session_string(self) -> None:
        pool = TelegramPool()
        worker_id = uuid4()
        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)
        mock_session = MagicMock()

        with (
            patch("app.state.TelegramClient", return_value=mock_client),
            patch("app.state.StringSession", return_value=mock_session),
        ):
            client = await pool.get_or_create(
                worker_id=worker_id,
                bot_token="fake:token",
                session_string="fake_session_string",
                api_id=12345,
                api_hash="fakehash",
            )

        assert client is mock_client
        # With session_string, it should call connect() not start()
        mock_client.connect.assert_awaited_once()
        mock_client.start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_remove_disconnects_client(self) -> None:
        pool = TelegramPool()
        worker_id = uuid4()
        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        with patch("app.state.TelegramClient", return_value=mock_client):
            await pool.get_or_create(
                worker_id=worker_id,
                bot_token="fake:token",
                session_string=None,
                api_id=12345,
                api_hash="fakehash",
            )

        assert pool.active_count == 1
        await pool.remove(worker_id)
        assert pool.active_count == 0
        mock_client.disconnect.assert_awaited()

    @pytest.mark.asyncio
    async def test_remove_nonexistent_is_noop(self) -> None:
        pool = TelegramPool()
        await pool.remove(uuid4())  # Should not raise
        assert pool.active_count == 0

    @pytest.mark.asyncio
    async def test_shutdown_disconnects_all(self) -> None:
        pool = TelegramPool()
        mock_clients = []

        for _ in range(3):
            worker_id = uuid4()
            mock_client = AsyncMock()
            mock_client.is_connected = MagicMock(return_value=True)
            mock_clients.append(mock_client)

            with patch("app.state.TelegramClient", return_value=mock_client):
                await pool.get_or_create(
                    worker_id=worker_id,
                    bot_token="fake:token",
                    session_string=None,
                    api_id=12345,
                    api_hash="fakehash",
                )

        assert pool.active_count == 3
        await pool.shutdown()
        assert pool.active_count == 0
        for mc in mock_clients:
            mc.disconnect.assert_awaited()

    def test_semaphore_created_per_worker(self) -> None:
        pool = TelegramPool()
        w1, w2 = uuid4(), uuid4()
        s1 = pool.semaphore(w1)
        s2 = pool.semaphore(w2)
        assert isinstance(s1, asyncio.Semaphore)
        assert s1 is not s2
        # Same worker returns same semaphore
        assert pool.semaphore(w1) is s1


# ── validate_and_register_worker tests ──────────────────────────


class TestValidateAndRegisterWorker:
    """Tests for app.services.storage.validate_and_register_worker."""

    @pytest.mark.asyncio
    async def test_valid_bot_token_returns_session_string(self) -> None:
        """Happy path: valid bot_token + chat_id → session_string."""
        mock_client = AsyncMock()
        mock_entity = MagicMock()
        mock_entity.title = "Test Channel"
        mock_client.get_entity.return_value = mock_entity

        mock_session = MagicMock()
        mock_session.save.return_value = "1BVtsOJcBu_session_data"
        mock_client.session = mock_session

        with patch("app.services.storage.TelegramClient", return_value=mock_client):
            from app.services.storage import validate_and_register_worker

            result = await validate_and_register_worker(
                bot_token="123456:ABC-valid-token",
                chat_id=-1001234567890,
            )

        assert result == "1BVtsOJcBu_session_data"
        mock_client.start.assert_awaited_once_with(bot_token="123456:ABC-valid-token")
        mock_client.get_entity.assert_awaited_once_with(-1001234567890)
        mock_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_bot_token_raises_valueerror(self) -> None:
        """Invalid bot_token → ValueError('Invalid or expired bot_token')."""
        from telethon.errors import AccessTokenInvalidError

        mock_client = AsyncMock()
        mock_client.start.side_effect = AccessTokenInvalidError(request=MagicMock())

        with patch("app.services.storage.TelegramClient", return_value=mock_client):
            from app.services.storage import validate_and_register_worker

            with pytest.raises(ValueError, match="Invalid or expired bot_token"):
                await validate_and_register_worker(
                    bot_token="invalid:token",
                    chat_id=-1001234567890,
                )

        mock_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_expired_bot_token_raises_valueerror(self) -> None:
        """Expired bot_token → ValueError('Invalid or expired bot_token')."""
        from telethon.errors import AccessTokenExpiredError

        mock_client = AsyncMock()
        mock_client.start.side_effect = AccessTokenExpiredError(request=MagicMock())

        with patch("app.services.storage.TelegramClient", return_value=mock_client):
            from app.services.storage import validate_and_register_worker

            with pytest.raises(ValueError, match="Invalid or expired bot_token"):
                await validate_and_register_worker(
                    bot_token="expired:token",
                    chat_id=-1001234567890,
                )

        mock_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_banned_bot_raises_valueerror(self) -> None:
        """Banned bot → ValueError('Bot account is banned')."""
        from telethon.errors import UserDeactivatedBanError

        mock_client = AsyncMock()
        mock_client.start.side_effect = UserDeactivatedBanError(request=MagicMock())

        with patch("app.services.storage.TelegramClient", return_value=mock_client):
            from app.services.storage import validate_and_register_worker

            with pytest.raises(ValueError, match="Bot account is banned"):
                await validate_and_register_worker(
                    bot_token="banned:token",
                    chat_id=-1001234567890,
                )

        mock_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_deactivated_bot_raises_valueerror(self) -> None:
        """Deactivated bot → ValueError('Bot account is banned')."""
        from telethon.errors import UserDeactivatedError

        mock_client = AsyncMock()
        mock_client.start.side_effect = UserDeactivatedError(request=MagicMock())

        with patch("app.services.storage.TelegramClient", return_value=mock_client):
            from app.services.storage import validate_and_register_worker

            with pytest.raises(ValueError, match="Bot account is banned"):
                await validate_and_register_worker(
                    bot_token="deactivated:token",
                    chat_id=-1001234567890,
                )

        mock_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_channel_access_raises_valueerror(self) -> None:
        """Bot without channel access → ValueError('Bot does not have access to the channel')."""
        from telethon.errors import ChannelPrivateError

        mock_client = AsyncMock()
        mock_client.get_entity.side_effect = ChannelPrivateError(request=MagicMock())

        with patch("app.services.storage.TelegramClient", return_value=mock_client):
            from app.services.storage import validate_and_register_worker

            with pytest.raises(
                ValueError, match="Bot does not have access to the channel"
            ):
                await validate_and_register_worker(
                    bot_token="valid:token",
                    chat_id=-1001234567890,
                )

        mock_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_channel_invalid_raises_valueerror(self) -> None:
        """Invalid channel → ValueError('Bot does not have access to the channel')."""
        from telethon.errors import ChannelInvalidError

        mock_client = AsyncMock()
        mock_client.get_entity.side_effect = ChannelInvalidError(request=MagicMock())

        with patch("app.services.storage.TelegramClient", return_value=mock_client):
            from app.services.storage import validate_and_register_worker

            with pytest.raises(
                ValueError, match="Bot does not have access to the channel"
            ):
                await validate_and_register_worker(
                    bot_token="valid:token",
                    chat_id=-1001234567890,
                )

        mock_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_client_always_disconnected_on_error(self) -> None:
        """Client is always disconnected, even when start() raises."""
        mock_client = AsyncMock()
        mock_client.start.side_effect = RuntimeError("network error")

        with patch("app.services.storage.TelegramClient", return_value=mock_client):
            from app.services.storage import validate_and_register_worker

            with pytest.raises(ValueError):
                await validate_and_register_worker(
                    bot_token="fail:token",
                    chat_id=-1001234567890,
                )

        # disconnect() is in finally block, so always called
        mock_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unresolvable_entity_raises_valueerror(self) -> None:
        """Telethon raises plain ValueError for unresolvable entities."""
        mock_client = AsyncMock()
        mock_client.get_entity.side_effect = ValueError(
            "Could not find the input entity"
        )

        with patch("app.services.storage.TelegramClient", return_value=mock_client):
            from app.services.storage import validate_and_register_worker

            with pytest.raises(
                ValueError, match="Bot does not have access to the channel"
            ):
                await validate_and_register_worker(
                    bot_token="valid:token",
                    chat_id=-1001234567890,
                )


@pytest.mark.asyncio
async def test_concurrent_reconnect_calls_connect_once():
    """Concurrent get_or_create on a disconnected client must call connect() exactly once."""
    TelegramPool.reset()
    pool = TelegramPool.get_instance()
    worker_id = uuid4()

    connect_call_count = 0

    async def slow_connect():
        nonlocal connect_call_count
        await asyncio.sleep(0)  # yield control to let other coroutine run
        connect_call_count += 1
        # After connect, client reports as connected
        mock_client.is_connected.return_value = True

    mock_client = MagicMock()
    mock_client.is_connected.return_value = False
    mock_client.connect = AsyncMock(side_effect=slow_connect)
    pool._clients[worker_id] = mock_client

    # Two concurrent calls — both see disconnected client
    await asyncio.gather(
        pool.get_or_create(worker_id, "token", "session", 123, "hash"),
        pool.get_or_create(worker_id, "token", "session", 123, "hash"),
    )

    assert connect_call_count == 1, (
        f"connect() called {connect_call_count} times, expected 1"
    )
    TelegramPool.reset()


@pytest.mark.asyncio
async def test_three_concurrent_reconnects_call_connect_once():
    """Three concurrent callers on a disconnected client must call connect() exactly once."""
    TelegramPool.reset()
    pool = TelegramPool.get_instance()
    worker_id = uuid4()

    connect_call_count = 0

    async def slow_connect():
        nonlocal connect_call_count
        await asyncio.sleep(0)
        connect_call_count += 1
        # After connect, client reports as connected
        mock_client.is_connected.return_value = True

    mock_client = MagicMock()
    mock_client.is_connected.return_value = False
    mock_client.connect = AsyncMock(side_effect=slow_connect)
    pool._clients[worker_id] = mock_client

    await asyncio.gather(
        pool.get_or_create(worker_id, "token", "session", 123, "hash"),
        pool.get_or_create(worker_id, "token", "session", 123, "hash"),
        pool.get_or_create(worker_id, "token", "session", 123, "hash"),
    )

    assert connect_call_count == 1, (
        f"connect() called {connect_call_count} times, expected 1"
    )
    TelegramPool.reset()
