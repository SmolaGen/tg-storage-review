"""Integration tests for Storage and Worker endpoints (Phase 3).

Covers:
- POST /storages — create storage
- GET /storages — list user storages with worker_count
- POST /storages/{id}/workers — add worker (mocked Telegram validation)
- GET /storages/{id}/workers — list workers
- Ownership isolation (404 for other user's storage)
- Invalid bot_token returns 400
- TelegramPool lifespan initialization
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4


import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.main import app
from app.models.base import Base
from app.models.storage import Storage
from app.models.storage_worker import StorageWorker
from app.models.user import User
from app.services.auth import create_access_token
from app.state import TelegramPool
from tests.conftest import test_engine, test_session_factory


# ── Helpers ──────────────────────────────────────────────────────


async def _create_user(session: AsyncSession, user_id: int = 100) -> User:
    """Insert a test user directly into the DB."""
    user = User(id=user_id, first_name="Test", username="testuser")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


def _auth_headers(user_id: int) -> dict[str, str]:
    """Generate valid Bearer auth headers for a user."""
    token = create_access_token(user_id=user_id, telegram_id=user_id)
    return {"Authorization": f"Bearer {token}"}


# ── Fixtures ─────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Create all tables before each test, drop after."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    TelegramPool.reset()


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    async with test_session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    """AsyncClient with test DB override and mocked TelegramPool lifespan."""

    async def override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = override_get_session

    # Patch the lifespan worker loading to avoid real Telegram connections
    with patch("app.main.async_session", test_session_factory):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac

    app.dependency_overrides.clear()


# ── POST /storages ───────────────────────────────────────────────


class TestCreateStorage:
    """POST /storages endpoint tests."""

    @pytest.mark.asyncio
    async def test_create_storage_success(self, client, db_session):
        """Creates a storage and returns 201 with storage data."""
        user = await _create_user(db_session)
        headers = _auth_headers(user.id)

        response = await client.post(
            "/storages",
            json={"name": "My Channel", "chat_id": -1001234567890},
            headers=headers,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "My Channel"
        assert data["chat_id"] == -1001234567890
        assert "id" in data
        assert "created_at" in data

    @pytest.mark.asyncio
    async def test_create_storage_no_auth(self, client):
        """Returns 401 without auth header."""
        response = await client.post(
            "/storages",
            json={"name": "Test", "chat_id": -1001234567890},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_create_storage_empty_name(self, client, db_session):
        """Returns 422 for empty name."""
        user = await _create_user(db_session)
        headers = _auth_headers(user.id)

        response = await client.post(
            "/storages",
            json={"name": "", "chat_id": -1001234567890},
            headers=headers,
        )
        assert response.status_code == 422


# ── GET /storages ────────────────────────────────────────────────


class TestListStorages:
    """GET /storages endpoint tests."""

    @pytest.mark.asyncio
    async def test_list_storages_empty(self, client, db_session):
        """Returns empty list when user has no storages."""
        user = await _create_user(db_session)
        headers = _auth_headers(user.id)

        response = await client.get("/storages", headers=headers)
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_list_storages_with_worker_count(self, client, db_session):
        """Returns storages with correct worker_count."""
        user = await _create_user(db_session)
        headers = _auth_headers(user.id)

        # Create storage directly in DB
        storage = Storage(user_id=user.id, name="Test Storage", chat_id=-100123)
        db_session.add(storage)
        await db_session.commit()
        await db_session.refresh(storage)

        # Add two workers directly
        for i in range(2):
            worker = StorageWorker(
                storage_id=storage.id,
                bot_token=f"fake:token{i}",
                session_string=f"session{i}",
            )
            db_session.add(worker)
        await db_session.commit()

        response = await client.get("/storages", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "Test Storage"
        assert data[0]["worker_count"] == 2

    @pytest.mark.asyncio
    async def test_list_storages_only_own(self, client, db_session):
        """User only sees their own storages, not other users'."""
        user1 = await _create_user(db_session, user_id=100)
        user2 = await _create_user(db_session, user_id=200)

        # Create one storage for each user
        s1 = Storage(user_id=user1.id, name="User1 Storage", chat_id=-100111)
        s2 = Storage(user_id=user2.id, name="User2 Storage", chat_id=-100222)
        db_session.add_all([s1, s2])
        await db_session.commit()

        # user1 should only see their own
        response = await client.get("/storages", headers=_auth_headers(user1.id))
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "User1 Storage"


# ── POST /storages/{id}/workers ──────────────────────────────────


class TestAddWorker:
    """POST /storages/{storage_id}/workers endpoint tests."""

    @pytest.mark.asyncio
    async def test_add_worker_success(self, client, db_session):
        """Valid bot_token creates worker with session_string via mocked validation."""
        user = await _create_user(db_session)
        headers = _auth_headers(user.id)

        # Create storage
        storage = Storage(user_id=user.id, name="Test", chat_id=-100123)
        db_session.add(storage)
        await db_session.commit()
        await db_session.refresh(storage)

        # Mock validate_and_register_worker to return a session string
        with patch(
            "app.api.storages.validate_and_register_worker",
            new_callable=AsyncMock,
            return_value="mock_session_string_data",
        ):
            response = await client.post(
                f"/storages/{storage.id}/workers",
                json={"bot_token": "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"},
                headers=headers,
            )

        assert response.status_code == 201
        data = response.json()
        assert data["storage_id"] == str(storage.id)
        assert data["has_session"] is True
        assert "id" in data
        assert "created_at" in data

    @pytest.mark.asyncio
    async def test_add_worker_foreign_storage_404(self, client, db_session):
        """Adding worker to another user's storage returns 404."""
        user1 = await _create_user(db_session, user_id=100)
        user2 = await _create_user(db_session, user_id=200)

        # Storage belongs to user2
        storage = Storage(user_id=user2.id, name="User2 Storage", chat_id=-100222)
        db_session.add(storage)
        await db_session.commit()
        await db_session.refresh(storage)

        # user1 tries to add worker → 404
        response = await client.post(
            f"/storages/{storage.id}/workers",
            json={"bot_token": "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"},
            headers=_auth_headers(user1.id),
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Storage not found"

    @pytest.mark.asyncio
    async def test_add_worker_nonexistent_storage_404(self, client, db_session):
        """Adding worker to a non-existent storage returns 404."""
        user = await _create_user(db_session)
        headers = _auth_headers(user.id)

        fake_id = uuid4()
        response = await client.post(
            f"/storages/{fake_id}/workers",
            json={"bot_token": "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"},
            headers=headers,
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_add_worker_invalid_bot_token_400(self, client, db_session):
        """Invalid bot_token (validation raises ValueError) returns 400."""
        user = await _create_user(db_session)
        headers = _auth_headers(user.id)

        storage = Storage(user_id=user.id, name="Test", chat_id=-100123)
        db_session.add(storage)
        await db_session.commit()
        await db_session.refresh(storage)

        # Mock validate_and_register_worker to raise ValueError
        with patch(
            "app.api.storages.validate_and_register_worker",
            new_callable=AsyncMock,
            side_effect=ValueError("Invalid or expired bot_token"),
        ):
            response = await client.post(
                f"/storages/{storage.id}/workers",
                json={"bot_token": "invalid:bot_token_value"},
                headers=headers,
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid or expired bot_token"

    @pytest.mark.asyncio
    async def test_add_worker_no_channel_access_400(self, client, db_session):
        """Bot without channel access returns 400."""
        user = await _create_user(db_session)
        headers = _auth_headers(user.id)

        storage = Storage(user_id=user.id, name="Test", chat_id=-100123)
        db_session.add(storage)
        await db_session.commit()
        await db_session.refresh(storage)

        with patch(
            "app.api.storages.validate_and_register_worker",
            new_callable=AsyncMock,
            side_effect=ValueError("Bot does not have access to the channel"),
        ):
            response = await client.post(
                f"/storages/{storage.id}/workers",
                json={"bot_token": "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"},
                headers=headers,
            )

        assert response.status_code == 400
        assert "channel" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_add_worker_short_token_422(self, client, db_session):
        """bot_token shorter than min_length returns 422 validation error."""
        user = await _create_user(db_session)
        headers = _auth_headers(user.id)

        storage = Storage(user_id=user.id, name="Test", chat_id=-100123)
        db_session.add(storage)
        await db_session.commit()
        await db_session.refresh(storage)

        response = await client.post(
            f"/storages/{storage.id}/workers",
            json={"bot_token": "short"},
            headers=headers,
        )
        assert response.status_code == 422


# ── GET /storages/{id}/workers ───────────────────────────────────


class TestListWorkers:
    """GET /storages/{storage_id}/workers endpoint tests."""

    @pytest.mark.asyncio
    async def test_list_workers_empty(self, client, db_session):
        """Returns empty list for storage with no workers."""
        user = await _create_user(db_session)
        headers = _auth_headers(user.id)

        storage = Storage(user_id=user.id, name="Test", chat_id=-100123)
        db_session.add(storage)
        await db_session.commit()
        await db_session.refresh(storage)

        response = await client.get(f"/storages/{storage.id}/workers", headers=headers)
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_list_workers_returns_workers(self, client, db_session):
        """Returns list of workers with has_session flag."""
        user = await _create_user(db_session)
        headers = _auth_headers(user.id)

        storage = Storage(user_id=user.id, name="Test", chat_id=-100123)
        db_session.add(storage)
        await db_session.commit()
        await db_session.refresh(storage)

        # Add a worker with session
        w1 = StorageWorker(
            storage_id=storage.id,
            bot_token="fake:token1",
            session_string="session_data",
        )
        # Add a worker without session
        w2 = StorageWorker(
            storage_id=storage.id,
            bot_token="fake:token2",
            session_string=None,
        )
        db_session.add_all([w1, w2])
        await db_session.commit()

        response = await client.get(f"/storages/{storage.id}/workers", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

        # Check has_session values
        sessions = {w["has_session"] for w in data}
        assert True in sessions
        assert False in sessions

    @pytest.mark.asyncio
    async def test_list_workers_foreign_storage_404(self, client, db_session):
        """Listing workers on another user's storage returns 404."""
        user1 = await _create_user(db_session, user_id=100)
        user2 = await _create_user(db_session, user_id=200)

        storage = Storage(user_id=user2.id, name="User2 Storage", chat_id=-100222)
        db_session.add(storage)
        await db_session.commit()
        await db_session.refresh(storage)

        response = await client.get(
            f"/storages/{storage.id}/workers",
            headers=_auth_headers(user1.id),
        )
        assert response.status_code == 404


# ── TelegramPool lifespan ────────────────────────────────────────


class TestTelegramPoolLifespan:
    """TelegramPool is initialized from DB in lifespan and shut down on stop."""

    @pytest.mark.asyncio
    async def test_lifespan_initializes_pool(self, db_session):
        """Pool loads workers with session_strings from DB during startup."""
        TelegramPool.reset()

        user = await _create_user(db_session)
        storage = Storage(user_id=user.id, name="Test", chat_id=-100123)
        db_session.add(storage)
        await db_session.commit()
        await db_session.refresh(storage)

        worker = StorageWorker(
            storage_id=storage.id,
            bot_token="fake:token",
            session_string="fake_session_data",
        )
        db_session.add(worker)
        await db_session.commit()
        await db_session.refresh(worker)

        mock_client = AsyncMock()
        mock_client.is_connected.return_value = True

        from app.main import lifespan

        with (
            patch("app.main.async_session", test_session_factory),
            patch("app.state.TelegramClient", return_value=mock_client),
            patch("app.state.StringSession"),
            patch("app.main.engine") as mock_engine,
        ):
            mock_engine.dispose = AsyncMock()
            async with lifespan(app):
                await app.state.telegram_task
                pool = TelegramPool.get_instance()
                assert pool.active_count >= 1

        TelegramPool.reset()

    @pytest.mark.asyncio
    async def test_lifespan_shuts_down_pool(self, db_session):
        """Pool.shutdown() is called when app stops."""
        TelegramPool.reset()

        from app.main import lifespan

        with (
            patch("app.main.async_session", test_session_factory),
            patch("app.main.engine") as mock_engine,
        ):
            mock_engine.dispose = AsyncMock()
            mock_shutdown = AsyncMock()

            async with lifespan(app):
                pool = TelegramPool.get_instance()
                pool.shutdown = mock_shutdown

            mock_shutdown.assert_awaited_once()

        TelegramPool.reset()
