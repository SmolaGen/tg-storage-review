"""Pydantic schemas for Folder endpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class FolderCreate(BaseModel):
    name: str
    storage_id: UUID


class FolderRename(BaseModel):
    name: str


class FolderResponse(BaseModel):
    id: UUID
    name: str
    storage_id: UUID
    created_at: datetime
    file_count: int = 0

    model_config = ConfigDict(from_attributes=True)
