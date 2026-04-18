"""Unit & integration tests for download_file_stream, _download_one_chunk, get_download_metadata,
and GET /files/{file_id}/download endpoint.

Unit tests cover:
- download_file_stream with 3 chunks (multi-chunk, correct order)
- download_file_stream with 1 chunk
- download_file_stream with 0 chunks (empty file)
- _download_one_chunk FileReferenceExpiredError retry succeeds on re-fetch
- _download_one_chunk FileReferenceExpiredError exhausted (raises after re-fetch also fails)
- _download_one_chunk FloodWaitError retry (succeeds on second attempt)
- _download_one_chunk FloodWaitError exhausted (propagates after MAX_RETRIES)
- download_file_stream raises ValueError for not-uploaded file
- download_file_stream raises ValueError for nonexistent file
- _download_one_chunk raises ValueError when get_messages returns None (message deleted)

Integration tests cover:
- GET /files/{file_id}/download → 200 with correct headers and body
- GET /files/{file_id}/download no auth → 401
- GET /files/{file_id}/download not found → 404
- GET /files/{file_id}/download foreign user → 404
- GET /files/{file_id}/download not uploaded → 409
- GET /files/{file_id}/download empty file → 200 with empty body
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.main import app
from app.models.file import File
from app.models.file_chunk import FileChunk
from app.models.storage import Storage
from app.models.storage_worker import StorageWorker
from app.services.auth import create_access_token
from app.services.file import (
    MAX_RETRIES,
    _download_one_chunk,
    download_file_stream,
)
from app.state import TelegramPool
from telethon.errors import FileReferenceExpiredError, FloodWaitError
from tests.conftest import test_session_factory


# ── Helpers ──────────────────────────────────────────────────────


async def _seed_user(session: AsyncSession, user_id: int = 100):
    from app.models.user import User

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


async def _seed_uploaded_file(
    session: AsyncSession,
    storage: Storage,
    workers: list[StorageWorker],
    chunk_count: int = 3,
    is_uploaded: bool = True,
) -> tuple[File, list[FileChunk]]:
    """Create a File with chunk_count FileChunk rows assigned round-robin to workers."""
    file = File(
        storage_id=storage.id,
        name="download_test.bin",
        size=1024 * chunk_count,
        mime_type="application/octet-stream",
        is_uploaded=is_uploaded,
    )
    session.add(file)
    await session.commit()
    await session.refresh(file)

    chunks = []
    for i in range(chunk_count):
        worker = workers[i % len(workers)]
        chunk = FileChunk(
            file_id=file.id,
            worker_id=worker.id,
            position=i,
            tg_file_id=f"tgfile_{i}",
            message_id=1000 + i,
            size=1024,
        )
        session.add(chunk)
        chunks.append(chunk)

    await session.commit()
    for c in chunks:
        await session.refresh(c)
    return file, chunks


def _make_flood_error(seconds: int = 10):
    """Create a FloodWaitError with given seconds."""
    exc = FloodWaitError(request=None, capture=seconds)
    return exc


def _make_file_reference_expired_error():
    """Create a FileReferenceExpiredError."""
    exc = FileReferenceExpiredError(request=None)
    return exc


def _patch_pool_download(download_data: bytes = b"chunk_data"):
    """Patch TelegramPool for download tests.

    Returns (mock_pool, mock_client) where client.get_messages returns
    a mock message and client.download_media returns download_data.
    """
    mock_msg = MagicMock()
    mock_msg.id = 42

    mock_client = AsyncMock()
    mock_client.get_messages = AsyncMock(return_value=mock_msg)
    mock_client.download_media = AsyncMock(return_value=download_data)

    mock_pool = MagicMock(spec=TelegramPool)
    mock_pool.get_or_create = AsyncMock(return_value=mock_client)
    mock_pool.semaphore = MagicMock(side_effect=lambda wid: asyncio.Semaphore(3))

    return mock_pool, mock_client


# ── Fixtures ─────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    async with test_session_factory() as session:
        yield session


# ── Tests: download_file_stream ──────────────────────────────────


class TestDownloadFileStreamMultiChunk:
    """Multi-chunk download yields bytes in position order."""

    @pytest.mark.asyncio
    async def test_download_file_stream_multi_chunk(self, db_session: AsyncSession):
        """3 chunks downloaded in order, each yields correct data."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        workers = await _seed_workers(db_session, storage.id, count=2)
        file, chunks = await _seed_uploaded_file(
            db_session, storage, workers, chunk_count=3
        )

        # Each call to download_media returns position-specific data
        chunk_data = [b"data_chunk_0", b"data_chunk_1", b"data_chunk_2"]

        mock_pool, mock_client = _patch_pool_download()
        mock_client.download_media = AsyncMock(side_effect=chunk_data)

        with patch(
            "app.services.file.TelegramPool.get_instance", return_value=mock_pool
        ):
            collected = []
            async for data in download_file_stream(db_session, file.id, user.id):
                collected.append(data)

        assert len(collected) == 3
        assert collected == chunk_data


class TestDownloadFileStreamSingleChunk:
    """Single chunk download."""

    @pytest.mark.asyncio
    async def test_download_file_stream_single_chunk(self, db_session: AsyncSession):
        """1 chunk yields exactly one bytes object."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        workers = await _seed_workers(db_session, storage.id, count=1)
        file, _ = await _seed_uploaded_file(db_session, storage, workers, chunk_count=1)

        mock_pool, mock_client = _patch_pool_download(b"single_chunk_bytes")

        with patch(
            "app.services.file.TelegramPool.get_instance", return_value=mock_pool
        ):
            collected = []
            async for data in download_file_stream(db_session, file.id, user.id):
                collected.append(data)

        assert len(collected) == 1
        assert collected[0] == b"single_chunk_bytes"


class TestDownloadFileStreamEmpty:
    """Empty file (0 chunks) yields nothing."""

    @pytest.mark.asyncio
    async def test_download_file_stream_empty_file(self, db_session: AsyncSession):
        """0 chunks → generator yields nothing."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        workers = await _seed_workers(db_session, storage.id, count=1)
        file, _ = await _seed_uploaded_file(db_session, storage, workers, chunk_count=0)

        mock_pool, _ = _patch_pool_download()

        with patch(
            "app.services.file.TelegramPool.get_instance", return_value=mock_pool
        ):
            collected = []
            async for data in download_file_stream(db_session, file.id, user.id):
                collected.append(data)

        assert len(collected) == 0


class TestDownloadOneChunkFileReferenceExpired:
    """FileReferenceExpiredError retry on re-fetch."""

    @pytest.mark.asyncio
    async def test_download_one_chunk_file_reference_expired_retry(
        self, db_session: AsyncSession
    ):
        """First download_media raises FileReferenceExpiredError, re-fetch succeeds."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        workers = await _seed_workers(db_session, storage.id, count=1)
        worker = workers[0]
        file_id = uuid4()

        mock_msg = MagicMock()
        mock_msg.id = 42
        refreshed_msg = MagicMock()
        refreshed_msg.id = 43

        mock_pool, mock_client = _patch_pool_download()
        # get_messages: first call returns mock_msg, second (re-fetch) returns refreshed_msg
        mock_client.get_messages = AsyncMock(side_effect=[mock_msg, refreshed_msg])
        # download_media: first raises FileReferenceExpiredError, second succeeds
        fre_error = _make_file_reference_expired_error()
        mock_client.download_media = AsyncMock(
            side_effect=[fre_error, b"refreshed_data"]
        )

        with patch(
            "app.services.file.TelegramPool.get_instance", return_value=mock_pool
        ):
            result = await _download_one_chunk(
                pool=mock_pool,
                worker=worker,
                chat_id=-1001234,
                message_id=1000,
                file_id=file_id,
                position=0,
            )

        assert result == b"refreshed_data"
        assert mock_client.get_messages.await_count == 2
        assert mock_client.download_media.await_count == 2

    @pytest.mark.asyncio
    async def test_download_one_chunk_file_reference_expired_exhausted(
        self, db_session: AsyncSession
    ):
        """FileReferenceExpiredError on both attempts → re-fetch download also fails."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        workers = await _seed_workers(db_session, storage.id, count=1)
        worker = workers[0]
        file_id = uuid4()

        mock_msg = MagicMock()
        refreshed_msg = MagicMock()

        mock_pool, mock_client = _patch_pool_download()
        mock_client.get_messages = AsyncMock(side_effect=[mock_msg, refreshed_msg])
        # Both download_media calls raise FileReferenceExpiredError
        fre_error1 = _make_file_reference_expired_error()
        fre_error2 = _make_file_reference_expired_error()
        mock_client.download_media = AsyncMock(side_effect=[fre_error1, fre_error2])

        with patch(
            "app.services.file.TelegramPool.get_instance", return_value=mock_pool
        ):
            with pytest.raises(FileReferenceExpiredError):
                await _download_one_chunk(
                    pool=mock_pool,
                    worker=worker,
                    chat_id=-1001234,
                    message_id=1000,
                    file_id=file_id,
                    position=0,
                )


class TestDownloadOneChunkFloodWait:
    """FloodWaitError retry behavior in download."""

    @pytest.mark.asyncio
    async def test_download_one_chunk_flood_wait_retry(self, db_session: AsyncSession):
        """FloodWaitError on first get_messages, succeeds on second attempt."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        workers = await _seed_workers(db_session, storage.id, count=1)
        worker = workers[0]
        file_id = uuid4()

        flood_err = _make_flood_error(seconds=1)
        mock_msg = MagicMock()

        mock_pool, mock_client = _patch_pool_download()
        # First get_messages raises FloodWaitError, second succeeds
        mock_client.get_messages = AsyncMock(side_effect=[flood_err, mock_msg])
        mock_client.download_media = AsyncMock(return_value=b"flood_retry_data")

        with (
            patch(
                "app.services.file.TelegramPool.get_instance", return_value=mock_pool
            ),
            patch(
                "app.services.file.asyncio.sleep", new_callable=AsyncMock
            ) as mock_sleep,
        ):
            result = await _download_one_chunk(
                pool=mock_pool,
                worker=worker,
                chat_id=-1001234,
                message_id=1000,
                file_id=file_id,
                position=0,
            )

        assert result == b"flood_retry_data"
        mock_sleep.assert_awaited_once_with(1 + 5)
        assert mock_client.get_messages.await_count == 2

    @pytest.mark.asyncio
    async def test_download_one_chunk_flood_wait_exhausted(
        self, db_session: AsyncSession
    ):
        """FloodWaitError on all attempts → propagates."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        workers = await _seed_workers(db_session, storage.id, count=1)
        worker = workers[0]
        file_id = uuid4()

        mock_pool, mock_client = _patch_pool_download()
        # All retries raise FloodWaitError
        mock_client.get_messages = AsyncMock(
            side_effect=[_make_flood_error(seconds=2) for _ in range(MAX_RETRIES)]
        )

        with (
            patch(
                "app.services.file.TelegramPool.get_instance", return_value=mock_pool
            ),
            patch("app.services.file.asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(FloodWaitError):
                await _download_one_chunk(
                    pool=mock_pool,
                    worker=worker,
                    chat_id=-1001234,
                    message_id=1000,
                    file_id=file_id,
                    position=0,
                )


class TestDownloadFileStreamNotUploaded:
    """File with is_uploaded=FALSE raises ValueError."""

    @pytest.mark.asyncio
    async def test_download_file_stream_not_uploaded(self, db_session: AsyncSession):
        """Attempting to download a not-yet-uploaded file raises ValueError."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        workers = await _seed_workers(db_session, storage.id, count=1)
        file, _ = await _seed_uploaded_file(
            db_session, storage, workers, chunk_count=1, is_uploaded=False
        )

        with pytest.raises(ValueError, match="File not uploaded yet"):
            async for _ in download_file_stream(db_session, file.id, user.id):
                pass


class TestDownloadFileStreamNotFound:
    """Nonexistent file_id raises ValueError."""

    @pytest.mark.asyncio
    async def test_download_file_stream_not_found(self, db_session: AsyncSession):
        """Attempting to download a nonexistent file raises ValueError."""
        user = await _seed_user(db_session)

        with pytest.raises(ValueError, match="File not found"):
            async for _ in download_file_stream(db_session, uuid4(), user.id):
                pass


class TestDownloadOneChunkMessageDeleted:
    """get_messages returns None → ValueError."""

    @pytest.mark.asyncio
    async def test_download_one_chunk_message_deleted(self, db_session: AsyncSession):
        """get_messages returns None, raises ValueError about deleted message."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        workers = await _seed_workers(db_session, storage.id, count=1)
        worker = workers[0]
        file_id = uuid4()

        mock_pool, mock_client = _patch_pool_download()
        mock_client.get_messages = AsyncMock(return_value=None)

        with patch(
            "app.services.file.TelegramPool.get_instance", return_value=mock_pool
        ):
            with pytest.raises(ValueError, match="Message deleted"):
                await _download_one_chunk(
                    pool=mock_pool,
                    worker=worker,
                    chat_id=-1001234,
                    message_id=1000,
                    file_id=file_id,
                    position=0,
                )


# ══════════════════════════════════════════════════════════════════
# Integration tests: GET /files/{file_id}/download
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


class TestDownloadEndpointSuccess:
    """GET /files/{file_id}/download → 200 with streamed body and correct headers."""

    @pytest.mark.asyncio
    async def test_download_success_200(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        """Seed file with 2 chunks, mock Telegram download, assert headers and body."""
        user = await _seed_user(db_session, user_id=500)
        storage = await _seed_storage(db_session, user.id)
        workers = await _seed_workers(db_session, storage.id, count=1)
        file, chunks = await _seed_uploaded_file(
            db_session, storage, workers, chunk_count=2, is_uploaded=True
        )
        # Override file metadata for assertions
        file.name = "report.pdf"
        file.mime_type = "application/pdf"
        file.size = 2048
        await db_session.commit()
        await db_session.refresh(file)

        # Each chunk download returns known bytes
        chunk_data = [b"chunk_zero_data_", b"chunk_one_data__"]
        mock_pool, mock_client = _patch_pool_download()
        mock_client.download_media = AsyncMock(side_effect=chunk_data)

        with patch(
            "app.services.file.TelegramPool.get_instance", return_value=mock_pool
        ):
            resp = await api_client.get(
                f"/files/{file.id}/download",
                headers=_auth_headers(user.id),
            )

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert (
            resp.headers["content-disposition"]
            == "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"
        )
        assert resp.headers["content-length"] == "2048"
        assert resp.content == b"chunk_zero_data_chunk_one_data__"


class TestDownloadEndpointNoAuth:
    """GET /files/{file_id}/download without Bearer → 401."""

    @pytest.mark.asyncio
    async def test_download_no_auth_401(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        """No authorization header → 401."""
        resp = await api_client.get(f"/files/{uuid4()}/download")
        assert resp.status_code == 401


class TestDownloadEndpointNotFound:
    """GET /files/{file_id}/download for nonexistent file → 404."""

    @pytest.mark.asyncio
    async def test_download_not_found_404(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        """Nonexistent file_id → 404."""
        user = await _seed_user(db_session, user_id=501)

        resp = await api_client.get(
            f"/files/{uuid4()}/download",
            headers=_auth_headers(user.id),
        )
        assert resp.status_code == 404
        assert "File not found" in resp.json()["detail"]


class TestDownloadEndpointForeignUser:
    """GET /files/{file_id}/download for another user's file → 404."""

    @pytest.mark.asyncio
    async def test_download_foreign_user_404(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        """File belongs to user1, request as user2 → 404."""
        user1 = await _seed_user(db_session, user_id=502)
        user2 = await _seed_user(db_session, user_id=503)
        storage = await _seed_storage(db_session, user1.id)
        workers = await _seed_workers(db_session, storage.id, count=1)
        file, _ = await _seed_uploaded_file(
            db_session, storage, workers, chunk_count=1, is_uploaded=True
        )

        resp = await api_client.get(
            f"/files/{file.id}/download",
            headers=_auth_headers(user2.id),
        )
        assert resp.status_code == 404


class TestDownloadEndpointNotUploaded:
    """GET /files/{file_id}/download for file with is_uploaded=FALSE → 409."""

    @pytest.mark.asyncio
    async def test_download_not_uploaded_409(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        """File not yet uploaded → 409."""
        user = await _seed_user(db_session, user_id=504)
        storage = await _seed_storage(db_session, user.id)
        workers = await _seed_workers(db_session, storage.id, count=1)
        file, _ = await _seed_uploaded_file(
            db_session, storage, workers, chunk_count=1, is_uploaded=False
        )

        resp = await api_client.get(
            f"/files/{file.id}/download",
            headers=_auth_headers(user.id),
        )
        assert resp.status_code == 409
        assert "upload not complete" in resp.json()["detail"].lower()


class TestDownloadEndpointEmptyFile:
    """GET /files/{file_id}/download for empty file (0 chunks) → 200 with empty body."""

    @pytest.mark.asyncio
    async def test_download_empty_file_200(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        """0 chunks, is_uploaded=TRUE → 200 with empty body and Content-Length: 0."""
        user = await _seed_user(db_session, user_id=505)
        storage = await _seed_storage(db_session, user.id)
        workers = await _seed_workers(db_session, storage.id, count=1)
        file, _ = await _seed_uploaded_file(
            db_session, storage, workers, chunk_count=0, is_uploaded=True
        )
        # Set size=0 for empty file
        file.size = 0
        await db_session.commit()
        await db_session.refresh(file)

        mock_pool, _ = _patch_pool_download()

        with patch(
            "app.services.file.TelegramPool.get_instance", return_value=mock_pool
        ):
            resp = await api_client.get(
                f"/files/{file.id}/download",
                headers=_auth_headers(user.id),
            )

        assert resp.status_code == 200
        assert resp.headers["content-length"] == "0"
        assert resp.content == b""
