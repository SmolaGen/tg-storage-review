"""Pydantic schemas for Storage and Worker endpoints (Phase 3).

Covers:
- Storage CRUD (create, list, detail, delete)
- Worker CRUD (add, list, delete)
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ── Storage schemas ──────────────────────────────────────────────


class StorageCreate(BaseModel):
    """POST /storages — create a new storage channel."""

    name: str = Field(..., min_length=1, max_length=255, description="Display name")
    chat_id: int = Field(..., description="Telegram channel/group numeric ID")


class StorageResponse(BaseModel):
    """Single storage record returned from API."""

    id: UUID
    name: str
    chat_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StorageListResponse(BaseModel):
    """Storage record for list endpoint — includes worker_count."""

    id: UUID
    name: str
    chat_id: int
    created_at: datetime
    worker_count: int = Field(default=0, ge=0)

    model_config = ConfigDict(from_attributes=True)


class StorageDetailResponse(BaseModel):
    """GET /storages/{id} — storage with nested workers."""

    id: UUID
    name: str
    chat_id: int
    created_at: datetime
    workers: list[WorkerResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


# ── Worker schemas ───────────────────────────────────────────────


class WorkerCreate(BaseModel):
    """POST /storages/{id}/workers — register a new bot-worker."""

    bot_token: str = Field(
        ...,
        min_length=10,
        description="Telegram Bot API token (e.g. 123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11)",
    )


class WorkerResponse(BaseModel):
    """Single worker record returned from API."""

    id: UUID
    storage_id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkerDetailResponse(BaseModel):
    """Worker with session presence indicator (never expose raw session_string)."""

    id: UUID
    storage_id: UUID
    has_session: bool = Field(description="Whether a persisted StringSession exists")
    last_used_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# Forward-ref resolution for StorageDetailResponse
StorageDetailResponse.model_rebuild()
