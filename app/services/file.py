"""File upload/download service: chunking, parallel Telegram upload, streaming download.

upload_file splits data into ≤20 MB chunks, assigns workers round-robin,
uploads in parallel via asyncio.gather + per-worker semaphores, then
batch-inserts file_chunk rows and flips is_uploaded=TRUE atomically.

download_file_stream yields chunk bytes in position order, with
FileReferenceExpiredError re-fetch retry and FloodWaitError sleep+retry.

get_file_by_id returns a file with ownership verification for status polling.
get_download_metadata returns file metadata needed before starting a stream.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from io import BytesIO
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func
from telethon.errors import FileReferenceExpiredError, FloodWaitError

from app.config import settings
from app.models.file import File
from app.models.file_chunk import FileChunk
from app.models.storage import Storage
from app.models.storage_worker import StorageWorker
from app.services.folder import get_folder
from app.state import TelegramPool

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────

CHUNK_SIZE = 20 * 1024 * 1024  # 20 MB
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
MAX_RETRIES = 3


# ── Upload helpers ───────────────────────────────────────────────


async def _upload_one_chunk(
    pool: TelegramPool,
    worker: StorageWorker,
    chat_id: int,
    chunk_bytes: bytes,
    position: int,
    file_id: UUID,
) -> FileChunk:
    """Upload a single chunk to Telegram with retry on FloodWaitError.

    Acquires the per-worker semaphore, calls client.send_file, and returns
    an unsaved FileChunk ORM object on success.

    Raises on exhausted retries or non-FloodWait errors.
    """
    sem = pool.semaphore(worker.id)

    async with sem:
        client = await pool.get_or_create(
            worker_id=worker.id,
            bot_token=worker.bot_token,
            session_string=worker.session_string,
            api_id=settings.tg_api_id,
            api_hash=settings.tg_api_hash,
        )

        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                buf = BytesIO(chunk_bytes)
                buf.name = f"chunk_{position}.bin"

                msg = await client.send_file(
                    chat_id,
                    buf,
                    force_document=True,
                    file_size=len(chunk_bytes),
                )

                if msg.document is None:
                    raise ValueError(
                        f"send_file returned message without document "
                        f"(file_id={file_id}, position={position})"
                    )

                logger.info(
                    "chunk_uploaded file_id=%s position=%d worker_id=%s msg_id=%d",
                    file_id,
                    position,
                    worker.id,
                    msg.id,
                )

                return FileChunk(
                    file_id=file_id,
                    worker_id=worker.id,
                    position=position,
                    tg_file_id=str(msg.document.id),
                    message_id=msg.id,
                    size=len(chunk_bytes),
                )

            except FloodWaitError as exc:
                last_exc = exc
                wait_seconds = exc.seconds + 5
                logger.warning(
                    "flood_wait_retry file_id=%s worker_id=%s seconds=%d attempt=%d/%d",
                    file_id,
                    worker.id,
                    exc.seconds,
                    attempt,
                    MAX_RETRIES,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(wait_seconds)
                # On last attempt, fall through to raise

        # All retries exhausted
        raise last_exc  # type: ignore[misc]


# ── Ingest (shared upload pipeline) ──────────────────────────────


async def ingest_file(file_id: UUID, storage_id: UUID, data: bytes) -> None:
    """Upload raw bytes for an already-created File record.

    Splits *data* into ≤20 MB chunks, assigns workers round-robin,
    uploads in parallel via asyncio.gather + per-worker semaphores,
    batch-inserts file_chunk rows, and flips ``is_uploaded=TRUE``.

    Creates its own DB session — safe to call from background tasks or
    the Telegram bot handler (no FastAPI DI required).

    Raises on worker errors (caller is expected to catch and report).
    """
    from app.database import async_session  # local to avoid circular at module level

    async with async_session() as session:
        file_size = len(data)

        # Edge case: empty file — just flip is_uploaded
        if file_size == 0:
            await session.execute(
                update(File).where(File.id == file_id).values(is_uploaded=True)
            )
            await session.commit()
            logger.info("ingest_complete file_id=%s (empty file, 0 chunks)", file_id)
            return

        # Fetch storage for chat_id
        storage = await session.get(Storage, storage_id)
        if storage is None:
            raise ValueError(f"Storage not found: storage_id={storage_id}")

        # Split into chunks
        chunks: list[bytes] = []
        offset = 0
        while offset < file_size:
            chunks.append(data[offset : offset + CHUNK_SIZE])
            offset += CHUNK_SIZE

        logger.info(
            "ingest_start file_id=%s size=%d chunk_count=%d",
            file_id,
            file_size,
            len(chunks),
        )

        # Query workers (round-robin by last_used_at)
        stmt = (
            select(StorageWorker)
            .where(StorageWorker.storage_id == storage_id)
            .order_by(StorageWorker.last_used_at.asc().nulls_first())
        )
        result = await session.execute(stmt)
        workers = list(result.scalars().all())

        if not workers:
            raise ValueError(f"No workers available for storage_id={storage_id}")

        # Parallel upload
        pool = TelegramPool.get_instance()
        tasks = []
        used_worker_ids: set[UUID] = set()
        for i, chunk_bytes in enumerate(chunks):
            worker = workers[i % len(workers)]
            used_worker_ids.add(worker.id)
            tasks.append(
                _upload_one_chunk(
                    pool=pool,
                    worker=worker,
                    chat_id=storage.chat_id,
                    chunk_bytes=chunk_bytes,
                    position=i,
                    file_id=file_id,
                )
            )

        file_chunks: list[FileChunk] = await asyncio.gather(*tasks)

        # Batch insert chunks + flip is_uploaded
        session.add_all(file_chunks)
        await session.execute(
            update(File).where(File.id == file_id).values(is_uploaded=True)
        )

        # Update last_used_at for used workers
        await session.execute(
            update(StorageWorker)
            .where(StorageWorker.id.in_(used_worker_ids))
            .values(last_used_at=func.now())
        )
        await session.commit()

    logger.info("ingest_complete file_id=%s", file_id)


# ── Download helpers ──────────────────────────────────────────────


async def _download_one_chunk(
    pool: TelegramPool,
    worker: StorageWorker,
    chat_id: int,
    message_id: int,
    file_id: UUID,
    position: int,
) -> bytes:
    """Download a single chunk from Telegram with retry on FloodWait and FileReferenceExpired.

    Acquires the per-worker semaphore, fetches the message, downloads media bytes.
    On FileReferenceExpiredError: re-fetches the message once and retries download.
    On FloodWaitError: sleeps e.seconds + 5, up to MAX_RETRIES attempts.

    Raises ValueError if the message is deleted (get_messages returns None)
    or download_media returns None.
    """
    sem = pool.semaphore(worker.id)

    async with sem:
        client = await pool.get_or_create(
            worker_id=worker.id,
            bot_token=worker.bot_token,
            session_string=worker.session_string,
            api_id=settings.tg_api_id,
            api_hash=settings.tg_api_hash,
        )

        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                msg = await client.get_messages(chat_id, ids=message_id)
                if msg is None:
                    raise ValueError(
                        f"Message deleted: message_id={message_id}, "
                        f"file_id={file_id}, position={position}"
                    )

                try:
                    data = await client.download_media(msg, file=bytes)
                except FileReferenceExpiredError:
                    logger.warning(
                        "file_reference_retry file_id=%s position=%d worker_id=%s attempt=%d",
                        file_id,
                        position,
                        worker.id,
                        attempt,
                    )
                    # Re-fetch message to get fresh file reference
                    msg = await client.get_messages(chat_id, ids=message_id)
                    if msg is None:
                        raise ValueError(
                            f"Message deleted on re-fetch: message_id={message_id}, "
                            f"file_id={file_id}, position={position}"
                        )
                    data = await client.download_media(msg, file=bytes)

                if data is None:
                    raise ValueError(
                        f"download_media returned None: "
                        f"file_id={file_id}, position={position}"
                    )

                logger.info(
                    "chunk_downloaded file_id=%s position=%d worker_id=%s size=%d",
                    file_id,
                    position,
                    worker.id,
                    len(data),
                )
                return data

            except FloodWaitError as exc:
                last_exc = exc
                wait_seconds = exc.seconds + 5
                logger.warning(
                    "flood_wait_retry_download file_id=%s worker_id=%s seconds=%d attempt=%d/%d",
                    file_id,
                    worker.id,
                    exc.seconds,
                    attempt,
                    MAX_RETRIES,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(wait_seconds)
                # On last attempt, fall through to raise

        # All retries exhausted
        raise last_exc  # type: ignore[misc]


# ── Public API ───────────────────────────────────────────────────


async def download_file_stream(
    session: AsyncSession,
    file_id: UUID,
    user_id: int,
) -> AsyncIterator[bytes]:
    """Async generator yielding file chunk bytes in position order.

    Queries file_chunks joined with storage_workers, then downloads
    each chunk via the worker that uploaded it.

    Raises ValueError if file not found or not yet uploaded.
    """
    file = await get_file_by_id(session, file_id, user_id)
    if file is None:
        raise ValueError("File not found")
    if not file.is_uploaded:
        raise ValueError("File not uploaded yet")

    # Query chunks with worker info, ordered by position
    stmt = (
        select(FileChunk, StorageWorker)
        .join(StorageWorker, FileChunk.worker_id == StorageWorker.id)
        .where(FileChunk.file_id == file_id)
        .order_by(FileChunk.position.asc())
    )
    result = await session.execute(stmt)
    rows = result.all()

    logger.info(
        "download_start file_id=%s chunk_count=%d",
        file_id,
        len(rows),
    )

    # We need the storage chat_id
    storage = await session.get(Storage, file.storage_id)

    pool = TelegramPool.get_instance()

    for chunk, worker in rows:
        try:
            data = await _download_one_chunk(
                pool=pool,
                worker=worker,
                chat_id=storage.chat_id,
                message_id=chunk.message_id,
                file_id=file_id,
                position=chunk.position,
            )
            yield data
        except Exception as exc:
            logger.error(
                "download_failed file_id=%s position=%d error=%s",
                file_id,
                chunk.position,
                exc,
            )
            raise

    logger.info("download_complete file_id=%s", file_id)


async def download_file_bytes(
    session: AsyncSession,
    file_id: UUID,
    user_id: int,
) -> bytes:
    """Download all chunks and return as a single bytes object.

    Used for thumbnail generation where we need the full file in memory.
    """
    buf = bytearray()
    async for chunk in download_file_stream(session, file_id, user_id):
        buf.extend(chunk)
    return bytes(buf)


async def get_download_metadata(
    session: AsyncSession,
    file_id: UUID,
    user_id: int,
) -> tuple[str, int, str | None, bool]:
    """Return (name, size, mime_type, is_uploaded) for a file.

    Raises ValueError if file not found.
    """
    file = await get_file_by_id(session, file_id, user_id)
    if file is None:
        raise ValueError("File not found")
    return file.name, file.size, file.mime_type, file.is_uploaded


async def upload_file(
    session: AsyncSession,
    storage_id: UUID,
    user_id: int,
    filename: str,
    content_type: str | None,
    data: bytes,
) -> File:
    """Upload a file: create record, chunk, parallel-upload to Telegram, commit.

    Returns the File with is_uploaded=TRUE on success.
    Raises ValueError on ownership mismatch, no workers, or validation errors.
    """
    # ── 1. Verify storage ownership ──────────────────────────────
    storage = await session.get(Storage, storage_id)
    if storage is None or storage.user_id != user_id:
        raise ValueError("Storage not found or access denied")

    file_size = len(data)

    logger.info(
        "upload_start storage_id=%s user_id=%d filename=%s size=%d",
        storage_id,
        user_id,
        filename,
        file_size,
    )

    # ── 2. Create File row (is_uploaded=FALSE) ───────────────────
    file = File(
        storage_id=storage_id,
        name=filename,
        size=file_size,
        mime_type=content_type,
        is_uploaded=False,
    )
    session.add(file)
    await session.commit()
    await session.refresh(file)

    # ── 3. Edge case: empty file ─────────────────────────────────
    if file_size == 0:
        await session.execute(
            update(File).where(File.id == file.id).values(is_uploaded=True)
        )
        await session.commit()
        await session.refresh(file)
        logger.info("upload_complete file_id=%s (empty file, 0 chunks)", file.id)
        return file

    # ── 4. Split into chunks ─────────────────────────────────────
    chunks: list[bytes] = []
    offset = 0
    while offset < file_size:
        chunks.append(data[offset : offset + CHUNK_SIZE])
        offset += CHUNK_SIZE

    logger.info(
        "upload_start file_id=%s size=%d chunk_count=%d",
        file.id,
        file_size,
        len(chunks),
    )

    # ── 5. Query workers (round-robin by last_used_at) ───────────
    stmt = (
        select(StorageWorker)
        .where(StorageWorker.storage_id == storage_id)
        .order_by(StorageWorker.last_used_at.asc().nulls_first())
    )
    result = await session.execute(stmt)
    workers = list(result.scalars().all())

    if not workers:
        raise ValueError("No workers available")

    logger.info(
        "upload_start file_id=%s worker_count=%d",
        file.id,
        len(workers),
    )

    # ── 6. Assign workers round-robin and gather uploads ─────────
    pool = TelegramPool.get_instance()

    tasks = []
    used_worker_ids: set[UUID] = set()
    for i, chunk_bytes in enumerate(chunks):
        worker = workers[i % len(workers)]
        used_worker_ids.add(worker.id)
        tasks.append(
            _upload_one_chunk(
                pool=pool,
                worker=worker,
                chat_id=storage.chat_id,
                chunk_bytes=chunk_bytes,
                position=i,
                file_id=file.id,
            )
        )

    try:
        file_chunks: list[FileChunk] = await asyncio.gather(*tasks)
    except Exception as exc:
        logger.error("upload_failed file_id=%s error=%s", file.id, exc)
        raise

    # ── 7. Batch insert chunks ───────────────────────────────────
    session.add_all(file_chunks)
    await session.commit()

    # ── 8. Flip is_uploaded ──────────────────────────────────────
    await session.execute(
        update(File).where(File.id == file.id).values(is_uploaded=True)
    )
    await session.commit()
    await session.refresh(file)

    # ── 9. Update last_used_at for used workers ──────────────────
    await session.execute(
        update(StorageWorker)
        .where(StorageWorker.id.in_(used_worker_ids))
        .values(last_used_at=func.now())
    )
    await session.commit()

    logger.info("upload_complete file_id=%s", file.id)
    return file


async def get_file_by_id(
    session: AsyncSession,
    file_id: UUID,
    user_id: int,
) -> File | None:
    """Return a File if it belongs to the given user, else None.

    Joins files → storages to verify ownership.
    Excludes soft-deleted files (is_deleted=True).
    """
    stmt = (
        select(File)
        .join(Storage, File.storage_id == Storage.id)
        .where(
            File.id == file_id,
            Storage.user_id == user_id,
            File.is_deleted == False,  # noqa: E712
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_files(
    session: AsyncSession,
    user_id: int,
    storage_id: UUID,
    offset: int = 0,
    limit: int = 20,
    folder_id: UUID | None = None,
    root_only: bool = False,
    mime_category: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[list[File], int]:
    """Return paginated files for a storage with total count.

    Only returns uploaded, non-deleted files, ordered by created_at DESC.
    When folder_id is a UUID, returns files in that folder.
    When root_only=True and folder_id is None, returns root files (folder_id IS NULL).
    When both are unset (default), returns all files regardless of folder.
    mime_category filters by MIME prefix (e.g. "image", "video", "audio", "application/pdf").
    date_from / date_to filter by created_at (inclusive).
    """
    base_where = [
        File.storage_id == storage_id,
        Storage.user_id == user_id,
        File.is_uploaded == True,  # noqa: E712
        File.is_deleted == False,  # noqa: E712
    ]

    if folder_id is not None:
        base_where.append(File.folder_id == folder_id)
    elif root_only:
        base_where.append(File.folder_id.is_(None))

    if mime_category:
        if "/" in mime_category:
            base_where.append(File.mime_type == mime_category)
        else:
            base_where.append(File.mime_type.like(f"{mime_category}/%"))

    if date_from:
        base_where.append(File.created_at >= date_from)
    if date_to:
        base_where.append(File.created_at <= date_to)

    # Total count
    count_stmt = (
        select(func.count())
        .select_from(File)
        .join(Storage, File.storage_id == Storage.id)
        .where(*base_where)
    )
    total = (await session.execute(count_stmt)).scalar_one()

    # Paginated list
    list_stmt = (
        select(File)
        .join(Storage, File.storage_id == Storage.id)
        .where(*base_where)
        .order_by(File.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(list_stmt)
    files = list(result.scalars().all())

    return files, total


async def move_file(
    session: AsyncSession,
    file_id: UUID,
    user_id: int,
    folder_id: UUID | None,
) -> File:
    """Move a file to a folder (or to root if folder_id is None).

    Raises ValueError if file not found, folder not found, or storage mismatch.
    """
    file = await get_file_by_id(session, file_id, user_id)
    if file is None:
        raise ValueError("File not found")

    if folder_id is not None:
        folder = await get_folder(session, folder_id, user_id)
        if folder.storage_id != file.storage_id:
            raise ValueError("Folder belongs to a different storage")

    await session.execute(
        update(File).where(File.id == file_id).values(folder_id=folder_id)
    )
    await session.commit()
    await session.refresh(file)

    logger.info("move_file file_id=%s folder_id=%s", file_id, folder_id)
    return file


async def soft_delete_file(
    session: AsyncSession,
    file_id: UUID,
    user_id: int,
) -> File:
    """Soft-delete a file by setting is_deleted=True.

    Raises ValueError if the file doesn't exist or belongs to another user.
    """
    from app.services.thumbnail import evict_thumbnail_cache

    file = await get_file_by_id(session, file_id, user_id)
    if file is None:
        raise ValueError("File not found")

    await session.execute(
        update(File).where(File.id == file_id).values(is_deleted=True)
    )
    await session.commit()
    await session.refresh(file)

    evict_thumbnail_cache(file_id)
    logger.info("soft_delete file_id=%s user_id=%d", file_id, user_id)
    return file


async def find_duplicates(
    session: AsyncSession,
    storage_id: UUID,
    user_id: int,
) -> list[list[File]]:
    """Return groups of duplicate files (same name + size) in a storage.

    Each group contains 2+ files ordered by created_at ASC (oldest first).
    Only uploaded, non-deleted files are considered.
    Raises ValueError if storage not found or doesn't belong to user.
    """
    from sqlalchemy import tuple_

    # Find (name, size) pairs that appear more than once
    dup_pairs_stmt = (
        select(File.name, File.size)
        .join(Storage, File.storage_id == Storage.id)
        .where(
            File.storage_id == storage_id,
            Storage.user_id == user_id,
            File.is_uploaded == True,  # noqa: E712
            File.is_deleted == False,  # noqa: E712
        )
        .group_by(File.name, File.size)
        .having(func.count() > 1)
    )
    dup_pairs_result = await session.execute(dup_pairs_stmt)
    dup_pairs = dup_pairs_result.all()  # list of (name, size) tuples

    if not dup_pairs:
        return []

    # Fetch all files matching those (name, size) pairs
    files_stmt = (
        select(File)
        .join(Storage, File.storage_id == Storage.id)
        .where(
            File.storage_id == storage_id,
            Storage.user_id == user_id,
            File.is_uploaded == True,  # noqa: E712
            File.is_deleted == False,  # noqa: E712
            tuple_(File.name, File.size).in_(dup_pairs),
        )
        .order_by(File.name, File.size, File.created_at.asc())
    )
    files_result = await session.execute(files_stmt)
    all_files = list(files_result.scalars().all())

    # Group by (name, size)
    groups: dict[tuple[str, int], list[File]] = {}
    for f in all_files:
        key = (f.name, f.size)
        groups.setdefault(key, []).append(f)

    return list(groups.values())


async def bulk_delete_files(
    session: AsyncSession,
    file_ids: list[UUID],
    user_id: int,
) -> int:
    """Soft-delete multiple files in one query.

    Only deletes files that belong to the user (via storage ownership).
    Returns the count of actually deleted files.
    """
    from app.services.thumbnail import evict_thumbnail_cache

    if not file_ids:
        return 0

    result = await session.execute(
        update(File)
        .where(
            File.id.in_(file_ids),
            File.is_deleted == False,  # noqa: E712
            File.storage_id.in_(select(Storage.id).where(Storage.user_id == user_id)),
        )
        .values(is_deleted=True)
        .returning(File.id)
    )
    await session.commit()

    deleted_rows = result.all()
    for row in deleted_rows:
        evict_thumbnail_cache(row[0])

    deleted_count = len(deleted_rows)
    logger.info("bulk_delete user_id=%d count=%d", user_id, deleted_count)
    return deleted_count
