"""File upload, status, and download REST endpoints.

Endpoints:
- POST /files/upload              — accepts multipart file + storage_id, returns 202
- GET  /files/{file_id}           — poll file upload status
- GET  /files/{file_id}/download  — stream file contents
- GET  /files/{file_id}/thumbnail — WebP thumbnail (image or video poster)
- GET  /files/{file_id}/preview   — animated WebP preview (video only)
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from urllib.parse import quote
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_session
from app.limiter import limiter
from app.models.file import File
from app.models.storage_worker import StorageWorker
from app.models.user import User
from app.schemas.file import (
    BulkDeleteRequest,
    BulkDeleteResponse,
    DuplicateGroup,
    DuplicatesResponse,
    FileListResponse,
    FileStatusResponse,
    FileUploadResponse,
)
from app.services.file import (
    MAX_FILE_SIZE,
    bulk_delete_files,
    download_file_stream,
    find_duplicates,
    get_file_by_id,
    ingest_file,
    list_files,
    move_file,
    soft_delete_file,
)
from app.services.folder import get_folder
from app.services.storage import get_storage_by_id
from app.services.thumbnail import (
    ALLOWED_SIZES,
    DEFAULT_QUALITY,
    get_or_generate_thumbnail,
    get_or_generate_video_preview,
    pre_generate_from_data,
)

logger = logging.getLogger(__name__)


def _sanitize_filename(name: str) -> str:
    """Remove characters that break the Content-Disposition header value."""
    return re.sub(r'["\r\n]', "", name)


router = APIRouter()


# ── Background upload task ───────────────────────────────────────


async def _save_blurhash(file_id: UUID, blurhash_str: str) -> None:
    """Persist computed blurhash to the database."""
    from app.database import async_session
    from sqlalchemy import update

    async with async_session() as session:
        await session.execute(
            update(File).where(File.id == file_id).values(blurhash=blurhash_str)
        )
        await session.commit()
    logger.info("blurhash_saved file_id=%s", file_id)


async def _background_upload(
    file_id: UUID,
    storage_id: UUID,
    user_id: int,
    data: bytes,
    mime_type: str | None = None,
) -> None:
    """Run Telegram upload + thumbnail pre-generation in a background task.

    Delegates chunking to ``ingest_file()``.
    After upload completes, pre-warms thumbnail disk cache and saves blurhash.
    """
    try:
        await ingest_file(file_id, storage_id, data)
        logger.info("background_upload_success file_id=%s", file_id)
    except Exception as exc:
        logger.error("background_upload_failed file_id=%s error=%s", file_id, exc)
        return

    if mime_type and (mime_type.startswith("image/") or mime_type.startswith("video/")):
        try:
            blurhash_str = await asyncio.to_thread(
                pre_generate_from_data, file_id, mime_type, data
            )
            if blurhash_str:
                await _save_blurhash(file_id, blurhash_str)
        except Exception as exc:
            logger.warning("pre_generate_failed file_id=%s error=%s", file_id, exc)


# ── Endpoints ────────────────────────────────────────────────────


@router.get(
    "",
    response_model=FileListResponse,
)
async def list_files_endpoint(
    storage_id: UUID,
    folder_id: UUID | None = None,
    root_only: bool = Query(False),
    offset: int = 0,
    limit: int = 20,
    mime_category: str | None = Query(
        None,
        description="MIME prefix or full type, e.g. 'image', 'video', 'application/pdf'",
    ),
    date_from: datetime | None = Query(
        None, description="Filter by upload date (inclusive), ISO 8601"
    ),
    date_to: datetime | None = Query(
        None, description="Filter by upload date (inclusive), ISO 8601"
    ),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FileListResponse:
    """List files in a storage with pagination.

    Returns only uploaded, non-deleted files owned by the current user.
    When folder_id is set, returns files in that folder.
    When root_only=true, returns only root files (not in any folder).
    When neither is set, returns all files (backward-compatible default).
    """
    # Verify storage ownership (404, not 403 — consistent with D002)
    storage = await get_storage_by_id(session=session, storage_id=storage_id)
    if storage is None or storage.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage not found",
        )

    files, total = await list_files(
        session,
        current_user.id,
        storage_id,
        offset,
        limit,
        folder_id,
        root_only,
        mime_category,
        date_from,
        date_to,
    )

    logger.info(
        "list_files storage_id=%s user_id=%d count=%d total=%d",
        storage_id,
        current_user.id,
        len(files),
        total,
    )

    return FileListResponse(
        items=[FileStatusResponse.model_validate(f) for f in files],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/duplicates",
    response_model=DuplicatesResponse,
)
async def get_duplicates_endpoint(
    storage_id: UUID = Query(..., description="Storage to scan for duplicates"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DuplicatesResponse:
    """Return groups of duplicate files (same name + size) in a storage."""
    groups = await find_duplicates(session, storage_id, current_user.id)

    dup_groups = [
        DuplicateGroup(
            name=files[0].name,
            size=files[0].size,
            files=[FileStatusResponse.model_validate(f) for f in files],
        )
        for files in groups
    ]
    total_duplicates = sum(len(g.files) - 1 for g in dup_groups)

    return DuplicatesResponse(
        groups=dup_groups,
        total_groups=len(dup_groups),
        total_duplicates=total_duplicates,
    )


@router.delete(
    "/bulk",
    response_model=BulkDeleteResponse,
)
async def bulk_delete_endpoint(
    body: BulkDeleteRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BulkDeleteResponse:
    """Soft-delete multiple files by ID in one request."""
    deleted = await bulk_delete_files(session, body.file_ids, current_user.id)
    return BulkDeleteResponse(deleted=deleted)


@router.delete(
    "/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_file_endpoint(
    file_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Soft-delete a file. Returns 204 No Content on success."""
    try:
        await soft_delete_file(session, file_id, current_user.id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found",
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/upload",
    response_model=FileUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit("20/minute")
async def upload_file_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile,
    storage_id: UUID = Form(...),
    folder_id: UUID | None = Form(None),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FileUploadResponse:
    """Upload a file to Telegram storage.

    Reads the file into memory, validates, creates File record (is_uploaded=FALSE),
    returns 202 immediately, and uploads chunks to Telegram in the background.
    """
    # 1. Read file data
    data = await file.read()
    file_size = len(data)

    # 2. Size check
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum size is {MAX_FILE_SIZE} bytes.",
        )

    # 3. Verify storage ownership (404, not 403 — consistent with D002)
    storage = await get_storage_by_id(session=session, storage_id=storage_id)
    if storage is None or storage.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage not found",
        )

    # 4. Check workers available
    result = await session.execute(
        select(StorageWorker).where(StorageWorker.storage_id == storage_id).limit(1)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No workers available for upload",
        )

    # 5. Validate folder ownership and storage match
    if folder_id is not None:
        try:
            folder = await get_folder(session, folder_id, current_user.id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Folder not found",
            )
        if folder.storage_id != storage_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Folder belongs to a different storage",
            )

    # 6. Create File row (is_uploaded=FALSE)
    file_record = File(
        storage_id=storage_id,
        folder_id=folder_id,
        name=file.filename or "unnamed",
        size=file_size,
        mime_type=file.content_type,
        is_uploaded=False,
    )
    session.add(file_record)
    await session.commit()
    await session.refresh(file_record)

    # 6. Schedule background upload
    background_tasks.add_task(
        _background_upload,
        file_id=file_record.id,
        storage_id=storage_id,
        user_id=current_user.id,
        data=data,
        mime_type=file.content_type,
    )

    logger.info(
        "upload_accepted file_id=%s storage_id=%s size=%d",
        file_record.id,
        storage_id,
        file_size,
    )

    return FileUploadResponse(
        id=file_record.id,
        name=file_record.name,
        size=file_record.size,
    )


@router.get(
    "/{file_id}",
    response_model=FileStatusResponse,
)
async def get_file_status_endpoint(
    file_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FileStatusResponse:
    """Get file upload status for polling."""
    file_record = await get_file_by_id(
        session=session,
        file_id=file_id,
        user_id=current_user.id,
    )
    if file_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found",
        )

    return FileStatusResponse.model_validate(file_record)


@router.get(
    "/{file_id}/download",
)
async def download_file_endpoint(
    file_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Download a file as a streaming response.

    Assembles chunks from Telegram in position order and streams them
    back to the client with appropriate Content-Type, Content-Disposition,
    and Content-Length headers.
    """
    # 1. Look up file with ownership check
    file_record = await get_file_by_id(
        session=session,
        file_id=file_id,
        user_id=current_user.id,
    )
    if file_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found",
        )

    # 2. File must be fully uploaded
    if not file_record.is_uploaded:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="File upload not complete",
        )

    # 3. Build streaming response from async generator
    media_type = file_record.mime_type or "application/octet-stream"

    safe_name = _sanitize_filename(file_record.name)
    ascii_name = safe_name.encode("ascii", errors="ignore").decode("ascii") or "file"
    encoded_name = quote(safe_name)
    content_disposition = (
        f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}"
    )

    return StreamingResponse(
        content=download_file_stream(session, file_id, current_user.id),
        media_type=media_type,
        headers={
            "Content-Disposition": content_disposition,
            "Content-Length": str(file_record.size),
        },
    )


class FileMoveBody(BaseModel):
    folder_id: UUID | None = None


@router.patch(
    "/{file_id}/move",
    response_model=FileStatusResponse,
)
async def move_file_endpoint(
    file_id: UUID,
    body: FileMoveBody,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FileStatusResponse:
    """Move a file to a folder or to root (folder_id=null)."""
    try:
        file_record = await move_file(session, file_id, current_user.id, body.folder_id)
    except ValueError as exc:
        msg = str(exc)
        if msg == "Folder belongs to a different storage":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
    return FileStatusResponse.model_validate(file_record)


@router.get("/{file_id}/thumbnail")
@limiter.limit("120/minute")
async def thumbnail_endpoint(
    request: Request,
    file_id: UUID,
    size: int = Query(default=200),
    quality: int = Query(default=DEFAULT_QUALITY, ge=20, le=95),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Return a disk-cached WebP thumbnail for an image or video file.

    Images: resized via Pillow. Videos: first frame via ffmpeg.
    Cache hit returns in ~5ms (disk read). Cache miss downloads from Telegram.
    Allowed sizes: 200, 400, 800px.
    """
    if size not in ALLOWED_SIZES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"size must be one of {sorted(ALLOWED_SIZES)}",
        )

    file_record = await get_file_by_id(session, file_id, current_user.id)
    if file_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )
    if not file_record.is_uploaded:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="File not ready"
        )

    mime = file_record.mime_type or ""
    if not mime.startswith(("image/", "video/")):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Not an image or video file",
        )

    etag = f'"{file_id}-{size}-{quality}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    thumb = await get_or_generate_thumbnail(
        session, file_id, current_user.id, size, quality, mime
    )
    if thumb is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot generate thumbnail",
        )

    return Response(
        content=thumb,
        media_type="image/webp",
        headers={
            "Cache-Control": "private, max-age=86400, immutable",
            "ETag": etag,
        },
    )


@router.get("/{file_id}/preview")
@limiter.limit("30/minute")
async def video_preview_endpoint(
    request: Request,
    file_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Return a disk-cached animated WebP preview (5 sec, 3 fps) for video files.

    Cache hit returns in ~5ms. Cache miss generates via ffmpeg (may take 10-30s).
    """
    file_record = await get_file_by_id(session, file_id, current_user.id)
    if file_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )
    if not file_record.is_uploaded:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="File not ready"
        )

    mime = file_record.mime_type or ""
    if not mime.startswith("video/"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Not a video file",
        )

    etag = f'"{file_id}-preview"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    preview = await get_or_generate_video_preview(session, file_id, current_user.id)
    if preview is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot generate video preview",
        )

    return Response(
        content=preview,
        media_type="image/webp",
        headers={
            "Cache-Control": "private, max-age=86400, immutable",
            "ETag": etag,
        },
    )
