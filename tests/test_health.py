"""Tests for the enhanced /health endpoint.

Covers:
- Happy path: DB ok, pool with connected workers → status=ok
- DB probe failure → status=degraded, db=error
- Pool has disconnected worker → status=degraded
- No workers in pool → status=ok (no pool is not degraded)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.state import TelegramPool


# --------------- fixtures ---------------

@pytest.fixture(autouse=True)
def _reset_pool():
    """Reset TelegramPool singleton before each test."""
    TelegramPool.reset()
    yield
    TelegramPool.reset()


# --------------- tests ---------------

@pytest.mark.asyncio
async def test_health_returns_ok(setup_db):
    """DB ok + connected workers → status=ok."""
    mock_pool = MagicMock(spec=TelegramPool)
    mock_pool.health.return_value = [
        {"worker_id": "abcd1234", "connected": True},
    ]

    with (
        patch("app.database.async_session") as mock_session_factory,
        patch.object(TelegramPool, "get_instance", return_value=mock_pool),
    ):
        # DB probe succeeds
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock()
        mock_session_factory.return_value = mock_session

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["telegram_pool"]["active_workers"] == 1
    assert body["telegram_pool"]["workers"][0]["connected"] is True


@pytest.mark.asyncio
async def test_health_db_error(setup_db):
    """DB probe raises → status=degraded, db=error."""
    mock_pool = MagicMock(spec=TelegramPool)
    mock_pool.health.return_value = []

    with (
        patch("app.database.async_session") as mock_session_factory,
        patch.object(TelegramPool, "get_instance", return_value=mock_pool),
    ):
        # DB probe fails
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(side_effect=Exception("DB connection refused"))
        mock_session_factory.return_value = mock_session

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db"] == "error"


@pytest.mark.asyncio
async def test_health_pool_disconnected_worker(setup_db):
    """Pool worker with connected=False → status=degraded."""
    mock_pool = MagicMock(spec=TelegramPool)
    mock_pool.health.return_value = [
        {"worker_id": "aaaa1111", "connected": True},
        {"worker_id": "bbbb2222", "connected": False},
    ]

    with (
        patch("app.database.async_session") as mock_session_factory,
        patch.object(TelegramPool, "get_instance", return_value=mock_pool),
    ):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock()
        mock_session_factory.return_value = mock_session

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db"] == "ok"
    assert body["telegram_pool"]["active_workers"] == 2


@pytest.mark.asyncio
async def test_health_no_workers(setup_db):
    """No workers → status=ok, active_workers=0."""
    mock_pool = MagicMock(spec=TelegramPool)
    mock_pool.health.return_value = []

    with (
        patch("app.database.async_session") as mock_session_factory,
        patch.object(TelegramPool, "get_instance", return_value=mock_pool),
    ):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock()
        mock_session_factory.return_value = mock_session

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["telegram_pool"]["active_workers"] == 0
    assert body["telegram_pool"]["workers"] == []
