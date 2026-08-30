"""Pydantic schemas for File upload and status polling endpoints.

Covers:
- FileUploadResponse — returned from POST /files/upload (202 Accepted)
- FileStatusResponse — returned from GET /files/{file_id}
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class FileUploadResponse(BaseModel):
    """POST /files/upload → 202 Accepted."""

    id: UUID
    name: str
    size: int

    model_config = ConfigDict(from_attributes=True)


class FileStatusResponse(BaseModel):
    """GET /files/{file_id} — file record with upload status."""

    id: UUID
    name: str
    size: int
    mime_type: str | None
    is_uploaded: bool
    created_at: datetime
    folder_id: UUID | None = None
    blurhash: str | None = None

    model_config = ConfigDict(from_attributes=True)


class FileListResponse(BaseModel):
    """GET /files — paginated list of files in a storage."""

    items: list[FileStatusResponse]
    total: int
    offset: int
    limit: int


class DuplicateGroup(BaseModel):
    """A group of files with identical name and size."""

    name: str
    size: int
    files: list[FileStatusResponse]


class DuplicatesResponse(BaseModel):
    """GET /files/duplicates — groups of duplicate files."""

    groups: list[DuplicateGroup]
    total_groups: int
    total_duplicates: int


class BulkDeleteRequest(BaseModel):
    """DELETE /files/bulk — list of file IDs to soft-delete."""

    file_ids: list[UUID]


class BulkDeleteResponse(BaseModel):
    """Response for bulk delete."""

    deleted: int
