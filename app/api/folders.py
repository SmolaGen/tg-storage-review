"""Folder CRUD endpoints."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_session
from app.models.user import User
from app.schemas.folder import FolderCreate, FolderRename, FolderResponse
from app.services.folder import (
    create_folder,
    delete_folder,
    list_folders,
    rename_folder,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("", response_model=FolderResponse, status_code=status.HTTP_201_CREATED)
async def create_folder_endpoint(
    body: FolderCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FolderResponse:
    try:
        folder = await create_folder(
            session, body.storage_id, body.name, current_user.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return FolderResponse(
        id=folder.id,
        name=folder.name,
        storage_id=folder.storage_id,
        created_at=folder.created_at,
        file_count=0,
    )


@router.get("", response_model=list[FolderResponse])
async def list_folders_endpoint(
    storage_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[FolderResponse]:
    try:
        rows = await list_folders(session, storage_id, current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return [
        FolderResponse(
            id=f.id,
            name=f.name,
            storage_id=f.storage_id,
            created_at=f.created_at,
            file_count=count,
        )
        for f, count in rows
    ]


@router.patch("/{folder_id}", response_model=FolderResponse)
async def rename_folder_endpoint(
    folder_id: UUID,
    body: FolderRename,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FolderResponse:
    try:
        folder = await rename_folder(session, folder_id, body.name, current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return FolderResponse(
        id=folder.id,
        name=folder.name,
        storage_id=folder.storage_id,
        created_at=folder.created_at,
    )


@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder_endpoint(
    folder_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        await delete_folder(session, folder_id, current_user.id)
    except ValueError as exc:
        detail = str(exc)
        status_code = (
            status.HTTP_409_CONFLICT
            if "contains" in detail
            else status.HTTP_404_NOT_FOUND
        )
        raise HTTPException(status_code=status_code, detail=detail)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
