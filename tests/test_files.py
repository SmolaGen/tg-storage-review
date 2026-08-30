"""Unit & integration tests for FileService and /files endpoints.

Unit tests cover:
- upload_file success (single worker, correct chunk insertion, is_uploaded flip)
- Multiple workers with round-robin distribution
- FloodWaitError retry (succeeds on second attempt)
- FloodWaitError exhausted (propagates after MAX_RETRIES)
- No workers → ValueError
- Single chunk (exactly CHUNK_SIZE bytes)
- Two chunks (CHUNK_SIZE + 1 bytes)
- Empty file (0 bytes, 0 chunks)
- get_file_by_id returns file for correct user
- get_file_by_id returns None for wrong user

Integration tests cover:
- POST /files/upload → 202 with file_id, name, size
- POST /files/upload no auth → 401
- POST /files/upload foreign storage → 404
- POST /files/upload no workers → 409
- POST /files/upload file too large → 413
- GET /files/{id} → 200 with is_uploaded field
- GET /files/{id} not found → 404
- GET /files/{id} foreign user → 404
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.main import app
from app.models.file import File
from app.models.file_chunk import FileChunk
from app.models.storage import Storage
from app.models.storage_worker import StorageWorker
from app.models.user import User
from app.services.auth import create_access_token
from app.services.file import (
    CHUNK_SIZE,
    MAX_FILE_SIZE,
    MAX_RETRIES,
    upload_file,
    get_file_by_id,
)
from app.state import TelegramPool
from telethon.errors import FloodWaitError
from tests.conftest import test_session_factory


# ── Helpers ──────────────────────────────────────────────────────


async def _seed_user(session: AsyncSession, user_id: int = 100) -> User:
    user = User(id=user_id, first_name="Test", username="testuser")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _seed_storage(
    session: AsyncSession, user_id: int, chat_id: int = -1001234
) -> Storage:
    storage = Storage(user_id=user_id, name="Test Storage", chat_id=chat_id)
    session.add(storage)
    await session.commit()
    await session.refresh(storage)
    return storage


async def _seed_workers(
    session: AsyncSession, storage_id, count: int = 1
) -> list[StorageWorker]:
    workers = []
    for i in range(count):
        w = StorageWorker(
            storage_id=storage_id,
            bot_token=f"fake:token{i}",
            session_string=f"session{i}",
        )
        session.add(w)
        workers.append(w)
    await session.commit()
    for w in workers:
        await session.refresh(w)
    return workers


def _mock_send_file_ok():
    """Return an AsyncMock for client.send_file that returns a valid message."""
    mock_doc = MagicMock()
    mock_doc.id = 999  # tg_file_id

    mock_msg = MagicMock()
    mock_msg.id = 42  # message_id
    mock_msg.document = mock_doc

    return AsyncMock(return_value=mock_msg)


def _make_flood_error(seconds: int = 10):
    """Create a FloodWaitError-like exception.

    Telethon FloodWaitError stores the wait seconds in .seconds attribute.
    We create a real-ish mock that has .seconds and can be raised.
    """
    from telethon.errors import FloodWaitError

    # FloodWaitError needs an RPC request; build a minimal one
    exc = FloodWaitError(request=None, capture=seconds)
    return exc


# ── Fixtures ─────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    async with test_session_factory() as session:
        yield session


def _patch_pool():
    """Patch TelegramPool.get_instance to return a mock pool with mock client."""
    mock_client = AsyncMock()
    mock_client.send_file = _mock_send_file_ok()

    mock_pool = MagicMock(spec=TelegramPool)
    mock_pool.get_or_create = AsyncMock(return_value=mock_client)
    mock_pool.semaphore = MagicMock(side_effect=lambda wid: asyncio.Semaphore(3))

    return mock_pool, mock_client


# ── Tests: upload_file ───────────────────────────────────────────


class TestUploadFileSuccess:
    """Happy-path upload with a single worker."""

    @pytest.mark.asyncio
    async def test_upload_file_success(self, db_session: AsyncSession):
        """File is created, chunks inserted, is_uploaded flipped to TRUE."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        workers = await _seed_workers(db_session, storage.id, count=1)

        data = b"x" * (CHUNK_SIZE + 100)  # 2 chunks
        mock_pool, mock_client = _patch_pool()

        with patch(
            "app.services.file.TelegramPool.get_instance", return_value=mock_pool
        ):
            file = await upload_file(
                session=db_session,
                storage_id=storage.id,
                user_id=user.id,
                filename="test.bin",
                content_type="application/octet-stream",
                data=data,
            )

        assert file.is_uploaded is True
        assert file.name == "test.bin"
        assert file.size == len(data)

        # Verify chunks
        result = await db_session.execute(
            select(FileChunk).where(FileChunk.file_id == file.id)
        )
        chunks = list(result.scalars().all())
        assert len(chunks) == 2
        assert {c.position for c in chunks} == {0, 1}
        assert all(c.worker_id == workers[0].id for c in chunks)

        # send_file called twice
        assert mock_client.send_file.await_count == 2


class TestUploadFileRoundRobin:
    """Multiple workers get chunks distributed round-robin."""

    @pytest.mark.asyncio
    async def test_upload_file_multiple_workers_round_robin(
        self, db_session: AsyncSession
    ):
        """3 workers, 5 chunks → workers[0] gets 0,3; workers[1] gets 1,4; workers[2] gets 2."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        workers = await _seed_workers(db_session, storage.id, count=3)

        data = b"y" * (CHUNK_SIZE * 4 + 100)  # 5 chunks
        mock_pool, mock_client = _patch_pool()

        with patch(
            "app.services.file.TelegramPool.get_instance", return_value=mock_pool
        ):
            file = await upload_file(
                session=db_session,
                storage_id=storage.id,
                user_id=user.id,
                filename="multi.bin",
                content_type=None,
                data=data,
            )

        assert file.is_uploaded is True

        result = await db_session.execute(
            select(FileChunk)
            .where(FileChunk.file_id == file.id)
            .order_by(FileChunk.position)
        )
        chunks = list(result.scalars().all())
        assert len(chunks) == 5

        # Round-robin: position i → workers[i % 3]
        for chunk in chunks:
            expected_worker = workers[chunk.position % 3]
            assert chunk.worker_id == expected_worker.id


class TestUploadFileFloodWait:
    """FloodWaitError retry behavior."""

    @pytest.mark.asyncio
    async def test_upload_file_flood_wait_retry(self, db_session: AsyncSession):
        """send_file raises FloodWaitError on first call, succeeds on second."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        await _seed_workers(db_session, storage.id, count=1)

        data = b"z" * CHUNK_SIZE  # exactly 1 chunk

        mock_pool, mock_client = _patch_pool()

        # First call raises FloodWaitError, second succeeds
        flood_err = _make_flood_error(seconds=1)
        ok_msg = _mock_send_file_ok().return_value
        mock_client.send_file = AsyncMock(side_effect=[flood_err, ok_msg])

        with (
            patch(
                "app.services.file.TelegramPool.get_instance", return_value=mock_pool
            ),
            patch(
                "app.services.file.asyncio.sleep", new_callable=AsyncMock
            ) as mock_sleep,
        ):
            file = await upload_file(
                session=db_session,
                storage_id=storage.id,
                user_id=user.id,
                filename="retry.bin",
                content_type=None,
                data=data,
            )

        assert file.is_uploaded is True
        # sleep called with e.seconds + 5
        mock_sleep.assert_awaited_once_with(1 + 5)
        assert mock_client.send_file.await_count == 2

    @pytest.mark.asyncio
    async def test_upload_file_flood_wait_exhausted(self, db_session: AsyncSession):
        """send_file always raises FloodWaitError → is_uploaded stays FALSE."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        await _seed_workers(db_session, storage.id, count=1)

        data = b"w" * CHUNK_SIZE

        mock_pool, mock_client = _patch_pool()

        # All retries fail
        flood_err = _make_flood_error(seconds=2)
        mock_client.send_file = AsyncMock(
            side_effect=[_make_flood_error(seconds=2) for _ in range(MAX_RETRIES)]
        )

        with (
            patch(
                "app.services.file.TelegramPool.get_instance", return_value=mock_pool
            ),
            patch("app.services.file.asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(FloodWaitError):
                await upload_file(
                    session=db_session,
                    storage_id=storage.id,
                    user_id=user.id,
                    filename="exhaust.bin",
                    content_type=None,
                    data=data,
                )

        # Verify is_uploaded is still FALSE
        result = await db_session.execute(select(File))
        files = list(result.scalars().all())
        assert len(files) == 1
        assert files[0].is_uploaded is False


class TestUploadFileEdgeCases:
    """Edge cases: no workers, exact chunk size, two chunks, empty file."""

    @pytest.mark.asyncio
    async def test_upload_file_no_workers(self, db_session: AsyncSession):
        """Storage with 0 workers raises ValueError."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        # No workers added

        data = b"a" * 100

        with pytest.raises(ValueError, match="No workers available"):
            await upload_file(
                session=db_session,
                storage_id=storage.id,
                user_id=user.id,
                filename="nope.bin",
                content_type=None,
                data=data,
            )

    @pytest.mark.asyncio
    async def test_upload_file_single_chunk(self, db_session: AsyncSession):
        """Exactly CHUNK_SIZE bytes → 1 chunk."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        await _seed_workers(db_session, storage.id, count=1)

        data = b"c" * CHUNK_SIZE
        mock_pool, mock_client = _patch_pool()

        with patch(
            "app.services.file.TelegramPool.get_instance", return_value=mock_pool
        ):
            file = await upload_file(
                session=db_session,
                storage_id=storage.id,
                user_id=user.id,
                filename="exact.bin",
                content_type=None,
                data=data,
            )

        assert file.is_uploaded is True

        result = await db_session.execute(
            select(FileChunk).where(FileChunk.file_id == file.id)
        )
        chunks = list(result.scalars().all())
        assert len(chunks) == 1
        assert chunks[0].position == 0

    @pytest.mark.asyncio
    async def test_upload_file_two_chunks(self, db_session: AsyncSession):
        """CHUNK_SIZE + 1 bytes → 2 chunks."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        await _seed_workers(db_session, storage.id, count=1)

        data = b"d" * (CHUNK_SIZE + 1)
        mock_pool, mock_client = _patch_pool()

        with patch(
            "app.services.file.TelegramPool.get_instance", return_value=mock_pool
        ):
            file = await upload_file(
                session=db_session,
                storage_id=storage.id,
                user_id=user.id,
                filename="two.bin",
                content_type=None,
                data=data,
            )

        assert file.is_uploaded is True

        result = await db_session.execute(
            select(FileChunk).where(FileChunk.file_id == file.id)
        )
        chunks = list(result.scalars().all())
        assert len(chunks) == 2
        assert {c.position for c in chunks} == {0, 1}

    @pytest.mark.asyncio
    async def test_upload_file_empty(self, db_session: AsyncSession):
        """Empty file (0 bytes) → 0 chunks, is_uploaded=TRUE immediately."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        await _seed_workers(db_session, storage.id, count=1)

        data = b""
        mock_pool, _ = _patch_pool()

        with patch(
            "app.services.file.TelegramPool.get_instance", return_value=mock_pool
        ):
            file = await upload_file(
                session=db_session,
                storage_id=storage.id,
                user_id=user.id,
                filename="empty.bin",
                content_type=None,
                data=data,
            )

        assert file.is_uploaded is True
        assert file.size == 0

        result = await db_session.execute(
            select(FileChunk).where(FileChunk.file_id == file.id)
        )
        chunks = list(result.scalars().all())
        assert len(chunks) == 0


class TestUploadFileOwnership:
    """Ownership mismatch raises ValueError."""

    @pytest.mark.asyncio
    async def test_upload_file_wrong_user(self, db_session: AsyncSession):
        """Uploading to another user's storage raises ValueError."""
        user1 = await _seed_user(db_session, user_id=100)
        user2 = await _seed_user(db_session, user_id=200)
        storage = await _seed_storage(db_session, user2.id)

        with pytest.raises(ValueError, match="Storage not found"):
            await upload_file(
                session=db_session,
                storage_id=storage.id,
                user_id=user1.id,
                filename="nope.bin",
                content_type=None,
                data=b"test",
            )


# ── Tests: get_file_by_id ───────────────────────────────────────


class TestGetFileById:
    """get_file_by_id with ownership check."""

    @pytest.mark.asyncio
    async def test_get_file_by_id_success(self, db_session: AsyncSession):
        """Returns file for the correct owner."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)

        file = File(
            storage_id=storage.id,
            name="owned.bin",
            size=100,
            mime_type="application/octet-stream",
            is_uploaded=True,
        )
        db_session.add(file)
        await db_session.commit()
        await db_session.refresh(file)

        result = await get_file_by_id(db_session, file.id, user.id)
        assert result is not None
        assert result.id == file.id
        assert result.name == "owned.bin"

    @pytest.mark.asyncio
    async def test_get_file_by_id_wrong_user(self, db_session: AsyncSession):
        """Returns None when user does not own the storage."""
        user1 = await _seed_user(db_session, user_id=100)
        user2 = await _seed_user(db_session, user_id=200)
        storage = await _seed_storage(db_session, user1.id)

        file = File(
            storage_id=storage.id,
            name="secret.bin",
            size=50,
            mime_type=None,
            is_uploaded=True,
        )
        db_session.add(file)
        await db_session.commit()
        await db_session.refresh(file)

        result = await get_file_by_id(db_session, file.id, user2.id)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_file_by_id_nonexistent(self, db_session: AsyncSession):
        """Returns None for a file ID that does not exist."""
        user = await _seed_user(db_session)

        result = await get_file_by_id(db_session, uuid4(), user.id)
        assert result is None


# ══════════════════════════════════════════════════════════════════
# Integration tests: /files endpoints
# ══════════════════════════════════════════════════════════════════


def _auth_headers(user_id: int) -> dict[str, str]:
    """Generate valid Bearer auth headers for a user."""
    token = create_access_token(user_id=user_id, telegram_id=user_id)
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def api_client(db_session: AsyncSession):
    """AsyncClient wired to test DB session."""

    async def override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = override_get_session

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


class TestUploadEndpoint:
    """POST /files/upload integration tests."""

    @pytest.mark.asyncio
    async def test_upload_endpoint_202(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        """POST multipart with file + storage_id → 202 with file_id, name, size."""
        user = await _seed_user(db_session, user_id=300)
        storage = await _seed_storage(db_session, user.id)
        await _seed_workers(db_session, storage.id, count=1)

        file_data = b"hello world upload test"
        resp = await api_client.post(
            "/files/upload",
            headers=_auth_headers(user.id),
            files={"file": ("test.txt", file_data, "text/plain")},
            data={"storage_id": str(storage.id)},
        )

        assert resp.status_code == 202
        body = resp.json()
        assert "id" in body
        assert body["name"] == "test.txt"
        assert body["size"] == len(file_data)

        # Verify File row was created in DB with is_uploaded=FALSE
        # (background task hasn't run yet in this sync test context)
        result = await db_session.execute(select(File))
        files = list(result.scalars().all())
        assert len(files) == 1
        assert files[0].is_uploaded is False

    @pytest.mark.asyncio
    async def test_upload_endpoint_no_auth_401(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        """POST without Bearer → 401."""
        resp = await api_client.post(
            "/files/upload",
            files={"file": ("test.txt", b"data", "text/plain")},
            data={"storage_id": str(uuid4())},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_upload_endpoint_foreign_storage_404(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        """POST with another user's storage_id → 404."""
        user1 = await _seed_user(db_session, user_id=301)
        user2 = await _seed_user(db_session, user_id=302)
        storage = await _seed_storage(db_session, user2.id)
        await _seed_workers(db_session, storage.id, count=1)

        resp = await api_client.post(
            "/files/upload",
            headers=_auth_headers(user1.id),
            files={"file": ("test.txt", b"data", "text/plain")},
            data={"storage_id": str(storage.id)},
        )
        assert resp.status_code == 404
        assert "Storage not found" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_upload_endpoint_nonexistent_storage_404(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        """POST with a UUID that doesn't exist → 404."""
        user = await _seed_user(db_session, user_id=303)

        resp = await api_client.post(
            "/files/upload",
            headers=_auth_headers(user.id),
            files={"file": ("test.txt", b"data", "text/plain")},
            data={"storage_id": str(uuid4())},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_upload_endpoint_no_workers_409(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        """POST to storage with 0 workers → 409."""
        user = await _seed_user(db_session, user_id=304)
        storage = await _seed_storage(db_session, user.id)
        # No workers added

        resp = await api_client.post(
            "/files/upload",
            headers=_auth_headers(user.id),
            files={"file": ("test.txt", b"data", "text/plain")},
            data={"storage_id": str(storage.id)},
        )
        assert resp.status_code == 409
        assert "No workers" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_upload_endpoint_file_too_large_413(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        """POST with data > 2GB → 413 (mock UploadFile.read to simulate)."""
        user = await _seed_user(db_session, user_id=305)
        storage = await _seed_storage(db_session, user.id)
        await _seed_workers(db_session, storage.id, count=1)

        # We can't actually send 2GB+ in tests. Patch the UploadFile class's
        # read method to return oversized bytes.
        original_read = None
        fake_read = AsyncMock(return_value=b"\x00" * (MAX_FILE_SIZE + 1))

        with patch("starlette.datastructures.UploadFile.read", fake_read):
            resp = await api_client.post(
                "/files/upload",
                headers=_auth_headers(user.id),
                files={"file": ("big.bin", b"x" * 100, "application/octet-stream")},
                data={"storage_id": str(storage.id)},
            )

        assert resp.status_code == 413
        assert "too large" in resp.json()["detail"].lower()


class TestGetFileStatusEndpoint:
    """GET /files/{file_id} integration tests."""

    @pytest.mark.asyncio
    async def test_get_file_status_success(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        """GET /files/{id} returns file with is_uploaded field."""
        user = await _seed_user(db_session, user_id=310)
        storage = await _seed_storage(db_session, user.id)

        file = File(
            storage_id=storage.id,
            name="status.bin",
            size=512,
            mime_type="application/octet-stream",
            is_uploaded=True,
        )
        db_session.add(file)
        await db_session.commit()
        await db_session.refresh(file)

        resp = await api_client.get(
            f"/files/{file.id}",
            headers=_auth_headers(user.id),
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == str(file.id)
        assert body["name"] == "status.bin"
        assert body["size"] == 512
        assert body["is_uploaded"] is True
        assert "created_at" in body

    @pytest.mark.asyncio
    async def test_get_file_status_not_found_404(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        """GET /files/{nonexistent} → 404."""
        user = await _seed_user(db_session, user_id=311)

        resp = await api_client.get(
            f"/files/{uuid4()}",
            headers=_auth_headers(user.id),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_file_status_foreign_user_404(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        """GET /files/{id} where file belongs to another user → 404."""
        user1 = await _seed_user(db_session, user_id=312)
        user2 = await _seed_user(db_session, user_id=313)
        storage = await _seed_storage(db_session, user1.id)

        file = File(
            storage_id=storage.id,
            name="secret.bin",
            size=100,
            mime_type=None,
            is_uploaded=False,
        )
        db_session.add(file)
        await db_session.commit()
        await db_session.refresh(file)

        # user2 tries to access user1's file
        resp = await api_client.get(
            f"/files/{file.id}",
            headers=_auth_headers(user2.id),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_file_status_no_auth_401(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        """GET /files/{id} without auth → 401."""
        resp = await api_client.get(f"/files/{uuid4()}")
        assert resp.status_code == 401


# ── AST mutation guard ───────────────────────────────────────────


import ast
import pathlib


def test_no_orm_mutation_in_file_service():
    """Ensure services/file.py uses update() instead of direct attribute assignment."""
    source = pathlib.Path("app/services/file.py").read_text()
    tree = ast.parse(source)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr in (
                    "is_uploaded",
                    "is_deleted",
                ):
                    violations.append(
                        f"line {node.lineno}: direct assignment to .{target.attr}"
                    )
    assert not violations, f"Found ORM mutations: {violations}"


def test_sanitize_filename_removes_injection_chars():
    from app.api.files import _sanitize_filename

    assert _sanitize_filename("normal.txt") == "normal.txt"
    assert _sanitize_filename('file"name.txt') == "filename.txt"
    assert _sanitize_filename("file\r\nname.txt") == "filename.txt"
    assert _sanitize_filename('a"b\rc\nd') == "abcd"
