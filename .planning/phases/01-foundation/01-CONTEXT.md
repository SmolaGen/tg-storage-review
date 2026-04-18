# Phase 1: Foundation - Context

**Gathered:** 2026-04-06
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 1 delivers the project skeleton: Docker Compose topology, PostgreSQL schema with all required columns (including `position`, `worker_id`, `message_id`, `tg_file_id` in file_chunks and `session_string` in storage_workers), Alembic migrations, and a working FastAPI app structure. No business logic — pure infrastructure.

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion

All implementation choices are at Claude's discretion — pure infrastructure phase.

Key constraints from research:
- `file_chunks` MUST have: `position INT NOT NULL`, `worker_id UUID NOT NULL`, `tg_file_id TEXT NOT NULL`, `message_id BIGINT NOT NULL` — retrofitting these later requires full rewrite
- `storage_workers` MUST have: `session_string TEXT` for StringSession (NOT SQLite file sessions)
- Use SQLAlchemy 2.0 async + asyncpg (NOT sync SQLAlchemy)
- Alembic with async template (`alembic init -t async`)
- FastAPI with lifespan context manager for DB pool and TelegramPool init
- Docker Compose: FastAPI (port 8000) + PostgreSQL (5432)
- Add `cryptg` to requirements from day one (3-10x Telegram upload speed)

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- None — greenfield project

### Established Patterns
- FastAPI + SQLAlchemy async: standard lifespan pattern
- Docker Compose: FastAPI + PostgreSQL minimal topology

### Integration Points
- All subsequent phases build on this DB schema — schema must be complete and correct
- TelegramPool initialized in FastAPI lifespan — all phases use it

</code_context>

<specifics>
## Specific Ideas

- Stack: FastAPI 0.135+, Telethon 1.42.0, cryptg 0.5.2, SQLAlchemy 2.0 async, asyncpg 0.31.0, PyJWT 2.12.1, alembic
- Schema tables: users, storages, storage_workers, files, file_chunks
- Health check endpoint: GET /health → {"status": "ok"}

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>
