"""Folder CRUD service."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file import File
from app.models.folder import Folder
from app.models.storage import Storage

logger = logging.getLogger(__name__)


async def _verify_storage_ownership(
    session: AsyncSession,
    storage_id: UUID,
    user_id: int,
) -> Storage:
    storage = await session.get(Storage, storage_id)
    if storage is None or storage.user_id != user_id:
        raise ValueError("Storage not found")
    return storage


async def create_folder(
    session: AsyncSession,
    storage_id: UUID,
    name: str,
    user_id: int,
) -> Folder:
    await _verify_storage_ownership(session, storage_id, user_id)

    folder = Folder(storage_id=storage_id, name=name)
    session.add(folder)
    await session.commit()
    await session.refresh(folder)

    logger.info("folder_created folder_id=%s storage_id=%s", folder.id, storage_id)
    return folder


async def list_folders(
    session: AsyncSession,
    storage_id: UUID,
    user_id: int,
) -> list[tuple[Folder, int]]:
    """Return folders with their non-deleted file counts."""
    await _verify_storage_ownership(session, storage_id, user_id)

    stmt = (
        select(Folder, func.count(File.id).label("file_count"))
        .outerjoin(
            File,
            (File.folder_id == Folder.id) & (File.is_deleted == False),  # noqa: E712
        )
        .where(Folder.storage_id == storage_id)
        .group_by(Folder.id)
        .order_by(Folder.name.asc())
    )
    result = await session.execute(stmt)
    return [(row.Folder, row.file_count) for row in result.all()]


async def rename_folder(
    session: AsyncSession,
    folder_id: UUID,
    name: str,
    user_id: int,
) -> Folder:
    folder = await _get_folder_with_ownership(session, folder_id, user_id)

    await session.execute(
        update(Folder).where(Folder.id == folder_id).values(name=name)
    )
    await session.commit()
    await session.refresh(folder)

    logger.info("folder_renamed folder_id=%s name=%s", folder_id, name)
    return folder


async def delete_folder(
    session: AsyncSession,
    folder_id: UUID,
    user_id: int,
) -> None:
    """Delete a folder. Raises ValueError if it contains any files."""
    await _get_folder_with_ownership(session, folder_id, user_id)

    count_result = await session.execute(
        select(func.count())
        .select_from(File)
        .where(File.folder_id == folder_id, File.is_deleted == False)  # noqa: E712
    )
    file_count = count_result.scalar_one()
    if file_count > 0:
        raise ValueError(
            f"Folder contains {file_count} file(s). Move or delete them first."
        )

    folder = await session.get(Folder, folder_id)
    if folder is not None:
        await session.delete(folder)
        await session.commit()

    logger.info("folder_deleted folder_id=%s", folder_id)


async def get_folder(
    session: AsyncSession,
    folder_id: UUID,
    user_id: int,
) -> Folder:
    return await _get_folder_with_ownership(session, folder_id, user_id)


async def _get_folder_with_ownership(
    session: AsyncSession,
    folder_id: UUID,
    user_id: int,
) -> Folder:
    stmt = (
        select(Folder)
        .join(Storage, Folder.storage_id == Storage.id)
        .where(Folder.id == folder_id, Storage.user_id == user_id)
    )
    result = await session.execute(stmt)
    folder = result.scalar_one_or_none()
    if folder is None:
        raise ValueError("Folder not found")
    return folder
