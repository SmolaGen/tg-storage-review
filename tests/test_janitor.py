"""Tests for janitor background job — cleanup_stale_files().

Covers:
- Stale incomplete file is soft-deleted
- Fresh incomplete file is skipped
- Uploaded file is skipped regardless of age
- Already-deleted file is not double-processed
- Multiple stale files cleaned in one pass
- Return value equals cleaned count
- Zero stale files returns 0
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.file import File
from app.models.storage import Storage
from app.models.user import User
from tests.conftest import test_session_factory


# --------------- helpers ---------------

async def _create_user_and_storage(session: AsyncSession) -> uuid.UUID:
    """Create a minimal User + Storage pair, return storage_id."""
    user = User(id=1, first_name="TestUser")
    session.add(user)
    await session.flush()

    storage = Storage(user_id=user.id, name="test-storage", chat_id=-100123)
    session.add(storage)
    await session.flush()
    return storage.id


async def _insert_file(
    session: AsyncSession,
    storage_id: uuid.UUID,
    *,
    is_uploaded: bool = False,
    is_deleted: bool = False,
    age_minutes: int = 60,
) -> File:
    """Insert a File with explicit created_at (SQLite-compatible)."""
    f = File(
        storage_id=storage_id,
        name=f"file-{uuid.uuid4().hex[:6]}.bin",
        size=1024,
        mime_type="application/octet-stream",
        is_uploaded=is_uploaded,
        is_deleted=is_deleted,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
    )
    session.add(f)
    await session.flush()
    return f


# --------------- tests ---------------

@pytest.mark.asyncio
@patch("app.database.async_session", test_session_factory)
async def test_cleanup_stale_incomplete_file(db_session: AsyncSession):
    """Stale incomplete file (>30 min, not uploaded, not deleted) → is_deleted=True."""
    from app.jobs.janitor import cleanup_stale_files

    storage_id = await _create_user_and_storage(db_session)
    f = await _insert_file(db_session, storage_id, is_uploaded=False, is_deleted=False, age_minutes=31)
    await db_session.commit()

    cleaned = await cleanup_stale_files()
    assert cleaned == 1

    await db_session.refresh(f)
    assert f.is_deleted is True


@pytest.mark.asyncio
@patch("app.database.async_session", test_session_factory)
async def test_cleanup_skips_fresh_incomplete_file(db_session: AsyncSession):
    """Fresh incomplete file (<30 min) is NOT deleted."""
    from app.jobs.janitor import cleanup_stale_files

    storage_id = await _create_user_and_storage(db_session)
    f = await _insert_file(db_session, storage_id, is_uploaded=False, is_deleted=False, age_minutes=5)
    await db_session.commit()

    cleaned = await cleanup_stale_files()
    assert cleaned == 0

    await db_session.refresh(f)
    assert f.is_deleted is False


@pytest.mark.asyncio
@patch("app.database.async_session", test_session_factory)
async def test_cleanup_skips_uploaded_file(db_session: AsyncSession):
    """Uploaded file is never cleaned regardless of age."""
    from app.jobs.janitor import cleanup_stale_files

    storage_id = await _create_user_and_storage(db_session)
    f = await _insert_file(db_session, storage_id, is_uploaded=True, is_deleted=False, age_minutes=120)
    await db_session.commit()

    cleaned = await cleanup_stale_files()
    assert cleaned == 0

    await db_session.refresh(f)
    assert f.is_deleted is False


@pytest.mark.asyncio
@patch("app.database.async_session", test_session_factory)
async def test_cleanup_skips_already_deleted(db_session: AsyncSession):
    """Already-deleted file is not double-processed."""
    from app.jobs.janitor import cleanup_stale_files

    storage_id = await _create_user_and_storage(db_session)
    f = await _insert_file(db_session, storage_id, is_uploaded=False, is_deleted=True, age_minutes=120)
    await db_session.commit()

    cleaned = await cleanup_stale_files()
    assert cleaned == 0

    await db_session.refresh(f)
    assert f.is_deleted is True  # still deleted, not touched


@pytest.mark.asyncio
@patch("app.database.async_session", test_session_factory)
async def test_cleanup_multiple_stale_files(db_session: AsyncSession):
    """3 stale + 1 fresh → cleans 3, leaves 1."""
    from app.jobs.janitor import cleanup_stale_files

    storage_id = await _create_user_and_storage(db_session)
    stale1 = await _insert_file(db_session, storage_id, age_minutes=60)
    stale2 = await _insert_file(db_session, storage_id, age_minutes=45)
    stale3 = await _insert_file(db_session, storage_id, age_minutes=90)
    fresh = await _insert_file(db_session, storage_id, age_minutes=5)
    await db_session.commit()

    cleaned = await cleanup_stale_files()
    assert cleaned == 3

    for f in (stale1, stale2, stale3):
        await db_session.refresh(f)
        assert f.is_deleted is True

    await db_session.refresh(fresh)
    assert fresh.is_deleted is False


@pytest.mark.asyncio
@patch("app.database.async_session", test_session_factory)
async def test_cleanup_returns_count(db_session: AsyncSession):
    """Return value matches the number of files cleaned."""
    from app.jobs.janitor import cleanup_stale_files

    storage_id = await _create_user_and_storage(db_session)
    for _ in range(5):
        await _insert_file(db_session, storage_id, age_minutes=60)
    await db_session.commit()

    cleaned = await cleanup_stale_files()
    assert cleaned == 5


@pytest.mark.asyncio
@patch("app.database.async_session", test_session_factory)
async def test_cleanup_zero_stale_returns_zero(db_session: AsyncSession):
    """No stale files → returns 0, no errors."""
    from app.jobs.janitor import cleanup_stale_files

    storage_id = await _create_user_and_storage(db_session)
    await _insert_file(db_session, storage_id, is_uploaded=True, age_minutes=120)
    await _insert_file(db_session, storage_id, is_uploaded=False, age_minutes=2)
    await db_session.commit()

    cleaned = await cleanup_stale_files()
    assert cleaned == 0
