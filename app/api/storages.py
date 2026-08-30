"""Storage and Worker REST endpoints (Phase 3).

Endpoints:
- POST /storages — create a new storage
- GET  /storages — list current user's storages (with worker_count)
- POST /storages/{storage_id}/workers — add a bot-worker (validates via Telegram)
- GET  /storages/{storage_id}/workers — list workers for a storage
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_session
from app.models.user import User
from app.schemas.storage import (
    StorageCreate,
    StorageListResponse,
    StorageResponse,
    WorkerCreate,
    WorkerDetailResponse,
)
from app.services.storage import (
    add_worker,
    create_storage,
    get_storage_by_id,
    list_storages,
    list_workers,
    validate_and_register_worker,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["storages"])


@router.post("", response_model=StorageResponse, status_code=status.HTTP_201_CREATED)
async def create_storage_endpoint(
    body: StorageCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StorageResponse:
    """Create a new storage channel for the current user."""
    storage = await create_storage(
        session=session,
        user_id=current_user.id,
        name=body.name,
        chat_id=body.chat_id,
    )
    return StorageResponse.model_validate(storage)


@router.get("", response_model=list[StorageListResponse])
async def list_storages_endpoint(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[StorageListResponse]:
    """List all storages for the current user, each annotated with worker_count."""
    rows = await list_storages(session=session, user_id=current_user.id)
    return [StorageListResponse(**row) for row in rows]


@router.post(
    "/{storage_id}/workers",
    response_model=WorkerDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_worker_endpoint(
    storage_id: UUID,
    body: WorkerCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WorkerDetailResponse:
    """Register a new bot-worker for the storage.

    Validates the bot_token against Telegram and verifies channel access.
    """
    # Check storage exists and belongs to current user
    storage = await get_storage_by_id(session=session, storage_id=storage_id)
    if storage is None or storage.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage not found",
        )

    # Validate bot_token + channel access via Telegram
    try:
        session_string = await validate_and_register_worker(
            bot_token=body.bot_token,
            chat_id=storage.chat_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    # Persist the worker
    worker = await add_worker(
        session=session,
        storage_id=storage_id,
        bot_token=body.bot_token,
        session_string=session_string,
    )

    return WorkerDetailResponse(
        id=worker.id,
        storage_id=worker.storage_id,
        has_session=worker.session_string is not None,
        last_used_at=worker.last_used_at,
        created_at=worker.created_at,
    )


@router.get("/{storage_id}/workers", response_model=list[WorkerDetailResponse])
async def list_workers_endpoint(
    storage_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[WorkerDetailResponse]:
    """List all workers for a storage owned by the current user."""
    # Check storage exists and belongs to current user
    storage = await get_storage_by_id(session=session, storage_id=storage_id)
    if storage is None or storage.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage not found",
        )

    workers = await list_workers(session=session, storage_id=storage_id)
    return [
        WorkerDetailResponse(
            id=w.id,
            storage_id=w.storage_id,
            has_session=w.session_string is not None,
            last_used_at=w.last_used_at,
            created_at=w.created_at,
        )
        for w in workers
    ]
