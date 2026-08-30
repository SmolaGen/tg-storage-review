"""Unit & integration tests for file management (list, soft-delete, is_deleted guard).

Unit tests (from T01):
- list_files: multiple files, pagination, is_deleted filter, is_uploaded filter, empty storage, total count
- soft_delete_file: success, nonexistent file, foreign user's file
- get_file_by_id: returns None for is_deleted=True file (regression)

Integration tests (from T02):
- GET /files: success with files, empty storage, pagination, filters deleted, filters not-uploaded, no auth → 401, foreign storage → 404
- DELETE /files/{file_id}: success → 204, nonexistent → 404, foreign user → 404, no auth → 401
- Cross-endpoint: deleted file invisible in GET /files/{file_id} and GET /files
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.main import app
from app.models.file import File
from app.models.storage import Storage
from app.models.storage_worker import StorageWorker
from app.models.user import User
from app.services.auth import create_access_token
from app.services.file import get_file_by_id, list_files, soft_delete_file
from tests.conftest import test_session_factory


# ── Helpers ──────────────────────────────────────────────────────


async def _seed_user(session: AsyncSession, user_id: int = 200) -> User:
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


async def _seed_file(
    session: AsyncSession,
    storage_id,
    name: str = "test.bin",
    is_uploaded: bool = True,
    is_deleted: bool = False,
    size: int = 1024,
) -> File:
    file = File(
        storage_id=storage_id,
        name=name,
        size=size,
        mime_type="application/octet-stream",
        is_uploaded=is_uploaded,
        is_deleted=is_deleted,
    )
    session.add(file)
    await session.commit()
    await session.refresh(file)
    return file


# ── Fixtures ─────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    async with test_session_factory() as session:
        yield session


# ══════════════════════════════════════════════════════════════════
# Tests: list_files
# ══════════════════════════════════════════════════════════════════


class TestListFilesMultiple:
    """list_files returns correct list of uploaded, non-deleted files."""

    @pytest.mark.asyncio
    async def test_list_files_returns_uploaded_files(self, db_session: AsyncSession):
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        f1 = await _seed_file(db_session, storage.id, name="a.bin")
        f2 = await _seed_file(db_session, storage.id, name="b.bin")
        f3 = await _seed_file(db_session, storage.id, name="c.bin")

        files, total = await list_files(db_session, user.id, storage.id)

        assert total == 3
        assert len(files) == 3
        # Ordered by created_at DESC — last created first
        names = [f.name for f in files]
        assert "a.bin" in names
        assert "b.bin" in names
        assert "c.bin" in names


class TestListFilesPagination:
    """list_files respects offset and limit."""

    @pytest.mark.asyncio
    async def test_list_files_pagination(self, db_session: AsyncSession):
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        for i in range(5):
            await _seed_file(db_session, storage.id, name=f"file_{i}.bin")

        files, total = await list_files(
            db_session, user.id, storage.id, offset=0, limit=2
        )
        assert total == 5
        assert len(files) == 2

        files2, total2 = await list_files(
            db_session, user.id, storage.id, offset=2, limit=2
        )
        assert total2 == 5
        assert len(files2) == 2

    @pytest.mark.asyncio
    async def test_list_files_offset_beyond_total(self, db_session: AsyncSession):
        """Offset beyond total returns empty list with correct total."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        await _seed_file(db_session, storage.id, name="only.bin")

        files, total = await list_files(
            db_session, user.id, storage.id, offset=100, limit=20
        )
        assert total == 1
        assert len(files) == 0


class TestListFilesFiltersDeleted:
    """list_files excludes is_deleted=True files."""

    @pytest.mark.asyncio
    async def test_list_files_excludes_deleted(self, db_session: AsyncSession):
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        await _seed_file(db_session, storage.id, name="visible.bin", is_deleted=False)
        await _seed_file(db_session, storage.id, name="deleted.bin", is_deleted=True)

        files, total = await list_files(db_session, user.id, storage.id)
        assert total == 1
        assert len(files) == 1
        assert files[0].name == "visible.bin"


class TestListFilesFiltersNotUploaded:
    """list_files excludes is_uploaded=False files."""

    @pytest.mark.asyncio
    async def test_list_files_excludes_not_uploaded(self, db_session: AsyncSession):
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        await _seed_file(db_session, storage.id, name="ready.bin", is_uploaded=True)
        await _seed_file(
            db_session, storage.id, name="pending.bin", is_uploaded=False
        )

        files, total = await list_files(db_session, user.id, storage.id)
        assert total == 1
        assert files[0].name == "ready.bin"


class TestListFilesEmptyStorage:
    """list_files for empty storage returns empty list and zero total."""

    @pytest.mark.asyncio
    async def test_list_files_empty_storage(self, db_session: AsyncSession):
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)

        files, total = await list_files(db_session, user.id, storage.id)
        assert total == 0
        assert files == []


class TestListFilesNonexistentStorage:
    """list_files for nonexistent storage_id returns empty list."""

    @pytest.mark.asyncio
    async def test_list_files_nonexistent_storage(self, db_session: AsyncSession):
        user = await _seed_user(db_session)

        files, total = await list_files(db_session, user.id, uuid4())
        assert total == 0
        assert files == []


# ══════════════════════════════════════════════════════════════════
# Tests: soft_delete_file
# ══════════════════════════════════════════════════════════════════


class TestSoftDeleteSuccess:
    """soft_delete_file sets is_deleted=True and returns the file."""

    @pytest.mark.asyncio
    async def test_soft_delete_success(self, db_session: AsyncSession):
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        file = await _seed_file(db_session, storage.id, name="to_delete.bin")

        result = await soft_delete_file(db_session, file.id, user.id)
        assert result.is_deleted is True
        assert result.id == file.id


class TestSoftDeleteNonexistentFile:
    """soft_delete_file raises ValueError for nonexistent file_id."""

    @pytest.mark.asyncio
    async def test_soft_delete_nonexistent(self, db_session: AsyncSession):
        user = await _seed_user(db_session)

        with pytest.raises(ValueError, match="File not found"):
            await soft_delete_file(db_session, uuid4(), user.id)


class TestSoftDeleteForeignUser:
    """soft_delete_file raises ValueError for another user's file."""

    @pytest.mark.asyncio
    async def test_soft_delete_foreign_user(self, db_session: AsyncSession):
        user1 = await _seed_user(db_session, user_id=201)
        user2 = await _seed_user(db_session, user_id=202)
        storage = await _seed_storage(db_session, user1.id)
        file = await _seed_file(db_session, storage.id, name="owned_by_user1.bin")

        with pytest.raises(ValueError, match="File not found"):
            await soft_delete_file(db_session, file.id, user2.id)


# ══════════════════════════════════════════════════════════════════
# Tests: get_file_by_id is_deleted guard (regression)
# ══════════════════════════════════════════════════════════════════


class TestGetFileByIdDeletedGuard:
    """get_file_by_id returns None for soft-deleted files."""

    @pytest.mark.asyncio
    async def test_get_file_by_id_returns_none_for_deleted(
        self, db_session: AsyncSession
    ):
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        file = await _seed_file(
            db_session, storage.id, name="deleted.bin", is_deleted=True
        )

        result = await get_file_by_id(db_session, file.id, user.id)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_file_by_id_returns_non_deleted(
        self, db_session: AsyncSession
    ):
        """Non-deleted file is returned normally (sanity check)."""
        user = await _seed_user(db_session)
        storage = await _seed_storage(db_session, user.id)
        file = await _seed_file(
            db_session, storage.id, name="alive.bin", is_deleted=False
        )

        result = await get_file_by_id(db_session, file.id, user.id)
        assert result is not None
        assert result.id == file.id


# ══════════════════════════════════════════════════════════════════
# Integration Tests: GET /files and DELETE /files/{file_id}
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


# ── GET /files tests ─────────────────────────────────────────────


class TestListFilesEndpointSuccess:
    """GET /files?storage_id=<uuid> returns FileListResponse with files."""

    @pytest.mark.asyncio
    async def test_list_files_success(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        user = await _seed_user(db_session, user_id=300)
        storage = await _seed_storage(db_session, user.id)
        f1 = await _seed_file(db_session, storage.id, name="alpha.bin")
        f2 = await _seed_file(db_session, storage.id, name="beta.bin")

        resp = await api_client.get(
            "/files", params={"storage_id": str(storage.id)},
            headers=_auth_headers(user.id),
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["offset"] == 0
        assert body["limit"] == 20
        assert len(body["items"]) == 2
        names = {item["name"] for item in body["items"]}
        assert names == {"alpha.bin", "beta.bin"}


class TestListFilesEndpointEmpty:
    """GET /files for empty storage returns empty list."""

    @pytest.mark.asyncio
    async def test_list_files_empty(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        user = await _seed_user(db_session, user_id=301)
        storage = await _seed_storage(db_session, user.id)

        resp = await api_client.get(
            "/files", params={"storage_id": str(storage.id)},
            headers=_auth_headers(user.id),
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []


class TestListFilesEndpointPagination:
    """GET /files respects offset and limit params."""

    @pytest.mark.asyncio
    async def test_list_files_pagination(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        user = await _seed_user(db_session, user_id=302)
        storage = await _seed_storage(db_session, user.id)
        for i in range(5):
            await _seed_file(db_session, storage.id, name=f"page_{i}.bin")

        resp = await api_client.get(
            "/files",
            params={"storage_id": str(storage.id), "offset": 0, "limit": 2},
            headers=_auth_headers(user.id),
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2
        assert body["offset"] == 0
        assert body["limit"] == 2


class TestListFilesEndpointFiltersDeleted:
    """GET /files excludes soft-deleted files."""

    @pytest.mark.asyncio
    async def test_list_files_filters_deleted(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        user = await _seed_user(db_session, user_id=303)
        storage = await _seed_storage(db_session, user.id)
        await _seed_file(db_session, storage.id, name="visible.bin")
        await _seed_file(db_session, storage.id, name="gone.bin", is_deleted=True)

        resp = await api_client.get(
            "/files", params={"storage_id": str(storage.id)},
            headers=_auth_headers(user.id),
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["name"] == "visible.bin"


class TestListFilesEndpointFiltersNotUploaded:
    """GET /files excludes in-progress uploads."""

    @pytest.mark.asyncio
    async def test_list_files_filters_not_uploaded(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        user = await _seed_user(db_session, user_id=304)
        storage = await _seed_storage(db_session, user.id)
        await _seed_file(db_session, storage.id, name="done.bin", is_uploaded=True)
        await _seed_file(db_session, storage.id, name="pending.bin", is_uploaded=False)

        resp = await api_client.get(
            "/files", params={"storage_id": str(storage.id)},
            headers=_auth_headers(user.id),
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["name"] == "done.bin"


class TestListFilesEndpointNoAuth:
    """GET /files without auth returns 401."""

    @pytest.mark.asyncio
    async def test_list_files_no_auth(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        resp = await api_client.get(
            "/files", params={"storage_id": str(uuid4())},
        )
        assert resp.status_code == 401


class TestListFilesEndpointForeignStorage:
    """GET /files with another user's storage returns 404."""

    @pytest.mark.asyncio
    async def test_list_files_foreign_storage(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        user1 = await _seed_user(db_session, user_id=305)
        user2 = await _seed_user(db_session, user_id=306)
        storage = await _seed_storage(db_session, user1.id)

        resp = await api_client.get(
            "/files", params={"storage_id": str(storage.id)},
            headers=_auth_headers(user2.id),
        )

        assert resp.status_code == 404


# ── DELETE /files/{file_id} tests ────────────────────────────────


class TestDeleteFileEndpointSuccess:
    """DELETE /files/{file_id} returns 204 and soft-deletes."""

    @pytest.mark.asyncio
    async def test_delete_success_204(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        user = await _seed_user(db_session, user_id=310)
        storage = await _seed_storage(db_session, user.id)
        file = await _seed_file(db_session, storage.id, name="to_remove.bin")

        resp = await api_client.delete(
            f"/files/{file.id}",
            headers=_auth_headers(user.id),
        )

        assert resp.status_code == 204
        assert resp.content == b""


class TestDeleteFileEndpointNotFound:
    """DELETE /files/{file_id} with nonexistent ID returns 404."""

    @pytest.mark.asyncio
    async def test_delete_nonexistent_404(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        user = await _seed_user(db_session, user_id=311)

        resp = await api_client.delete(
            f"/files/{uuid4()}",
            headers=_auth_headers(user.id),
        )

        assert resp.status_code == 404


class TestDeleteFileEndpointForeignUser:
    """DELETE /files/{file_id} for another user's file returns 404."""

    @pytest.mark.asyncio
    async def test_delete_foreign_user_404(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        user1 = await _seed_user(db_session, user_id=312)
        user2 = await _seed_user(db_session, user_id=313)
        storage = await _seed_storage(db_session, user1.id)
        file = await _seed_file(db_session, storage.id, name="user1_file.bin")

        resp = await api_client.delete(
            f"/files/{file.id}",
            headers=_auth_headers(user2.id),
        )

        assert resp.status_code == 404


class TestDeleteFileEndpointNoAuth:
    """DELETE /files/{file_id} without auth returns 401."""

    @pytest.mark.asyncio
    async def test_delete_no_auth_401(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        resp = await api_client.delete(f"/files/{uuid4()}")
        assert resp.status_code == 401


# ── Cross-endpoint tests ────────────────────────────────────────


class TestDeletedFileInvisibleAcrossEndpoints:
    """After DELETE, file is invisible in GET /files/{id} and GET /files."""

    @pytest.mark.asyncio
    async def test_deleted_invisible_in_status_and_list(
        self, db_session: AsyncSession, api_client: AsyncClient
    ):
        user = await _seed_user(db_session, user_id=320)
        storage = await _seed_storage(db_session, user.id)
        file = await _seed_file(db_session, storage.id, name="cross_check.bin")
        headers = _auth_headers(user.id)

        # Verify file is visible before deletion
        status_resp = await api_client.get(
            f"/files/{file.id}", headers=headers,
        )
        assert status_resp.status_code == 200

        list_resp = await api_client.get(
            "/files", params={"storage_id": str(storage.id)}, headers=headers,
        )
        assert list_resp.status_code == 200
        assert list_resp.json()["total"] == 1

        # Delete the file
        del_resp = await api_client.delete(
            f"/files/{file.id}", headers=headers,
        )
        assert del_resp.status_code == 204

        # File should be invisible in status endpoint
        status_resp2 = await api_client.get(
            f"/files/{file.id}", headers=headers,
        )
        assert status_resp2.status_code == 404

        # File should be invisible in list endpoint
        list_resp2 = await api_client.get(
            "/files", params={"storage_id": str(storage.id)}, headers=headers,
        )
        assert list_resp2.status_code == 200
        assert list_resp2.json()["total"] == 0
        assert list_resp2.json()["items"] == []
