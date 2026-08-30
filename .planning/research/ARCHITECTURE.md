# Architecture Patterns

**Domain:** Telegram-backed multi-tenant file storage service
**Researched:** 2026-04-06
**Confidence:** HIGH (official Telethon/FastAPI docs + MTProto spec + Pentaract reference)

---

## Recommended Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    HTTP Clients / TG Bot                │
└───────────┬──────────────────────────┬──────────────────┘
            │ REST API                 │ Bot webhook
            ▼                          ▼
┌───────────────────────────────────────────────────────┐
│                    FastAPI App                        │
│  ┌──────────┐  ┌──────────┐  ┌────────────────────┐  │
│  │  Auth    │  │  Files   │  │  Storages/Workers  │  │
│  │  Router  │  │  Router  │  │  Router            │  │
│  └────┬─────┘  └────┬─────┘  └────────┬───────────┘  │
│       │             │                 │               │
│  ┌────▼─────────────▼─────────────────▼───────────┐   │
│  │                 Services Layer                 │   │
│  │  AuthService  FileService  StorageService      │   │
│  └─────────────────────┬──────────────────────────┘   │
│                        │                              │
│  ┌─────────────────────▼──────────────────────────┐   │
│  │              TelegramPool                      │   │
│  │  {bot_token → TelegramClient}                  │   │
│  └─────────────────────┬──────────────────────────┘   │
└────────────────────────┼──────────────────────────────┘
                         │ MTProto (Telethon)
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
      [Bot A]         [Bot B]        [Bot C]
      (channel)      (channel)      (channel)
                  Telegram servers

┌──────────────────────────┐
│     PostgreSQL           │
│  users                   │
│  storages                │
│  storage_workers         │
│  files                   │
│  file_chunks             │
└──────────────────────────┘
```

---

## Component Boundaries

| Component | Responsibility | Communicates With |
|-----------|---------------|-------------------|
| **Auth Router** | Telegram Login Widget callback, JWT issue/refresh/revoke | AuthService |
| **Files Router** | Upload endpoint, Download endpoint, List, Delete | FileService, depends on JWT middleware |
| **Storages Router** | CRUD for storages (bot_token + chat_id), workers management | StorageService |
| **AuthService** | HMAC-SHA256 hash verification of TG Login data, JWT sign/verify, refresh token rotation | PostgreSQL (users table) |
| **FileService** | Orchestrates upload pipeline: chunk split → worker assignment → parallel send; orchestrates download: chunk fetch → stream assembly | TelegramPool, PostgreSQL (files, file_chunks) |
| **StorageService** | Validates bot_token via Telethon `get_me()`, registers worker, removes worker | TelegramPool, PostgreSQL (storages, storage_workers) |
| **TelegramPool** | Manages dict of `{bot_token: TelegramClient}`, connect/disconnect lifecycle, exposes `upload_chunk()` and `download_chunk()` primitives | Telethon, called by FileService |
| **ChunkingService** | Pure utility: splits `bytes` into 20 MB parts, assigns workers via round-robin | Stateless, used by FileService |
| **PostgreSQL** | Persistent state: users, file metadata, chunk location map | asyncpg pool, initialized in lifespan |
| **Telegram Bot** | Receives forwarded files from users, calls FileService.ingest() | FileService (via internal call, not HTTP) |

---

## PostgreSQL Schema

```sql
-- Users authenticated via Telegram Login
CREATE TABLE users (
    id          BIGINT PRIMARY KEY,          -- Telegram user_id
    username    TEXT,
    first_name  TEXT NOT NULL,
    last_name   TEXT,
    photo_url   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A storage = one private Telegram channel
CREATE TABLE storages (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    chat_id     BIGINT NOT NULL,             -- Telegram channel ID
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, chat_id)
);

-- A worker = one bot assigned to a storage (for load balancing)
CREATE TABLE storage_workers (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    storage_id  UUID NOT NULL REFERENCES storages(id) ON DELETE CASCADE,
    bot_token   TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Rolling window rate limit counter (no Redis needed, per Pentaract pattern)
    last_used_at TIMESTAMPTZ,
    UNIQUE (storage_id, bot_token)
);

-- File metadata; is_uploaded=FALSE until all chunks confirmed
CREATE TABLE files (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    storage_id  UUID NOT NULL REFERENCES storages(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    size        BIGINT NOT NULL,             -- bytes
    mime_type   TEXT,
    is_uploaded BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per 20 MB chunk; worker_id tells us WHICH bot holds file_id
CREATE TABLE file_chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id     UUID NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    worker_id   UUID NOT NULL REFERENCES storage_workers(id),
    position    INT NOT NULL,               -- 0-based ordering for reassembly
    tg_file_id  TEXT NOT NULL,              -- Telegram message_id or file_id
    size        BIGINT NOT NULL,
    UNIQUE (file_id, position)
);
```

**Key schema decisions:**
- `is_uploaded` flag (Pentaract pattern) prevents partially-uploaded files from being visible or downloadable.
- `file_chunks.worker_id` is mandatory — Telegram binds `file_id` to the uploading bot; download MUST use the same bot.
- Rate limiting state lives in `storage_workers.last_used_at` via SQL window queries — no Redis dependency.

---

## Data Flow: Upload

```
Client POST /files/upload  (multipart, Authorization: Bearer <jwt>)
    │
    ├─ FastAPI reads UploadFile as async stream (chunked read, avoid full RAM load)
    │
    ├─ FileService.upload()
    │   ├─ INSERT INTO files (is_uploaded=FALSE)
    │   ├─ ChunkingService.split(data) → List[bytes]  (each ≤ 20 MB)
    │   ├─ StorageService.assign_workers(storage_id, n_chunks)
    │   │       → round-robin over storage_workers, respecting last_used_at
    │   │
    │   ├─ asyncio.gather(*[
    │   │       TelegramPool.upload_chunk(bot_token, chat_id, chunk_bytes, position)
    │   │       for (chunk, worker) in zip(chunks, workers)
    │   │   ])
    │   │       Each gather task:
    │   │           client.send_file(chat_id, chunk_bytes) → tg_message_id
    │   │           INSERT INTO file_chunks (position, worker_id, tg_file_id)
    │   │
    │   └─ UPDATE files SET is_uploaded=TRUE WHERE id=...
    │
    └─ Return 201 {file_id, name, size}
```

**Upload runs entirely in an async background task** — the HTTP endpoint returns 202 Accepted immediately with the file_id, and the client polls `GET /files/{id}` until `is_uploaded=TRUE`. This avoids HTTP client timeouts for large files.

If any chunk fails: the file row stays `is_uploaded=FALSE` and is eligible for a cleanup job. Partial chunks already in Telegram are abandoned (not worth the complexity of partial retry in v1).

---

## Data Flow: Download

```
Client GET /files/{file_id}/download  (Authorization: Bearer <jwt>)
    │
    ├─ Verify JWT, verify file belongs to user's storage
    ├─ SELECT file_chunks WHERE file_id=? ORDER BY position ASC
    │       → list of (worker_id, tg_file_id) in order
    │
    ├─ return StreamingResponse(content=_stream_chunks(...), media_type=...)
    │
    └─ async generator _stream_chunks(chunks):
            for chunk in chunks:
                worker = TelegramPool.get(chunk.bot_token)
                data = await worker.download_media(chunk.tg_file_id, bytes=True)
                yield data
```

**Key constraints:**
- Download is sequential per chunk (not parallel) to maintain byte order for the client stream.
- Each chunk must be downloaded by the SAME bot that uploaded it — Telegram's MTProto binds `file_id` to the uploading bot/session.
- `StreamingResponse` with an async generator keeps server RAM usage to one chunk at a time (~20 MB peak).
- Parallelism is possible only with prefetch buffering (queue of size 2-3); defer to v2.

---

## TelegramPool: Design

```python
# Pseudo-structure (not final code)
class TelegramPool:
    _clients: dict[str, TelegramClient]  # bot_token → client

    async def get_or_create(self, bot_token: str) -> TelegramClient:
        if bot_token not in self._clients:
            client = TelegramClient(
                session=StringSession(),
                api_id=settings.TG_API_ID,
                api_hash=settings.TG_API_HASH,
                connection_retries=-1,   # infinite retries
            )
            await client.start(bot_token=bot_token)
            self._clients[bot_token] = client
        return self._clients[bot_token]

    async def remove(self, bot_token: str) -> None:
        client = self._clients.pop(bot_token, None)
        if client:
            await client.disconnect()

    async def shutdown(self) -> None:
        await asyncio.gather(*[c.disconnect() for c in self._clients.values()])
        self._clients.clear()
```

**Lifecycle:**
- Pool is instantiated once at application startup (FastAPI `lifespan` context manager).
- Clients are lazily created on first use (when a worker is added by a user).
- Pool shutdown is called in lifespan teardown, gracefully disconnecting all sessions.
- `StringSession` stores session data in-memory; no SQLite file per bot needed.
- All clients share the same asyncio event loop — Telethon requires this and it works correctly as long as all operations are inside `async def` functions in the same loop.

**Reconnection:**
- `connection_retries=-1` enables infinite reconnect on drop.
- For persistent production reliability, a health-check coroutine can periodically call `await client.get_me()` and recreate dead sessions.

---

## Auth Flow: Telegram Login Widget

```
Browser                 FastAPI               Telegram
   │                       │                      │
   ├── Click Login ─────────────────────────────►│
   │◄───────────── auth data (hash + fields) ────┤
   │                       │                      │
   ├── GET /auth/telegram/callback?{fields} ──►  │
   │       AuthService.verify():                  │
   │         data_check_string = sort+join fields │
   │         secret_key = SHA256(bot_token)        │
   │         expected = HMAC-SHA256(secret, dcs)  │
   │         assert expected == hash              │
   │         assert auth_date within 24h          │
   │                       │                      │
   │◄── 200 {access_token, refresh_token} ────────│
```

**JWT tokens:**
- `access_token`: HS256, 15-minute expiry, payload = `{sub: user_id, type: "access"}`.
- `refresh_token`: HS256, 30-day expiry, stored hashed (SHA-256) in `refresh_tokens` table for revocation.
- Token rotation: each `/auth/refresh` call issues a new pair and invalidates the old refresh token (delete by hash).

---

## Background Tasks vs Sync for Heavy Uploads

| Approach | When to Use | Rationale |
|----------|------------|-----------|
| **Async background task (asyncio.create_task)** | Upload to Telegram | Telethon is native asyncio; `create_task` keeps it in the same event loop, no thread overhead |
| **FastAPI BackgroundTasks** | Post-upload cleanup / notifications | Simple fire-and-forget after response is sent |
| **Starlette background in StreamingResponse** | Do NOT use for upload | BackgroundTask blocks StreamingResponse teardown — known FastAPI issue |
| **Thread pool (run_in_executor)** | Never for this project | Telethon is not thread-safe; mixing threads with asyncio clients causes race conditions |

**Recommended upload pattern:**
1. HTTP endpoint reads the file body into a temporary buffer (or streams it to ChunkingService directly).
2. Returns `202 Accepted` with `{file_id}`.
3. `asyncio.create_task(file_service.upload_to_telegram(file_id, chunks, workers))` runs concurrently.
4. Client polls `GET /files/{file_id}` — returns `{is_uploaded: false}` until complete.

---

## Docker Compose Service Topology

```yaml
services:
  api:
    build: .
    ports: ["8000:8000"]
    env_file: .env
    depends_on:
      db:
        condition: service_healthy
    networks: [internal]

  db:
    image: postgres:16-alpine
    volumes: [pgdata:/var/lib/postgresql/data]
    environment:
      POSTGRES_DB: tgstorage
      POSTGRES_USER: tgstorage
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U tgstorage"]
      interval: 5s
      timeout: 5s
      retries: 5
    # NOT exposed on host — internal only
    networks: [internal]

  bot:
    build: .
    command: python -m app.bot
    env_file: .env
    depends_on: [api, db]
    networks: [internal]

networks:
  internal:

volumes:
  pgdata:
```

**Notes:**
- `api` and `bot` share the same image; `bot` runs a different entrypoint.
- No Redis service — rate limiting is SQL-based (Pentaract pattern).
- No Nginx in development; add in production behind a reverse proxy.
- `db` not exposed to host to reduce attack surface.

---

## Suggested Build Order

Dependencies drive this ordering — each layer depends only on the layer above it being complete.

```
Phase 1: Foundation
    PostgreSQL schema + migrations (alembic)
    asyncpg pool + lifespan setup
    Settings (pydantic-settings)
    └── Unblocks: everything

Phase 2: Auth
    Telegram Login Widget verification (AuthService)
    JWT issue/refresh/revoke
    JWT middleware (Bearer extraction)
    └── Unblocks: all protected endpoints

Phase 3: TelegramPool + StorageService
    TelegramPool (connect, shutdown, lazy init)
    StorageService (add/remove storage, add/remove worker)
    Validate bot_token on registration
    └── Unblocks: FileService (needs clients and workers)

Phase 4: Upload Pipeline
    ChunkingService (split bytes)
    Worker assignment (round-robin + last_used_at)
    FileService.upload_to_telegram (asyncio.gather)
    POST /files/upload endpoint (202 + polling)
    └── Unblocks: Download (needs file_chunks rows)

Phase 5: Download Pipeline
    FileService._stream_chunks async generator
    GET /files/{id}/download StreamingResponse
    └── Unblocks: E2E test of full round-trip

Phase 6: Telegram Bot Interface
    Bot event handler for forwarded files
    Calls FileService.ingest() with file data
    └── Parallel path — no new Phase 4/5 dependencies

Phase 7: Hardening
    Cleanup job for is_uploaded=FALSE stuck files
    Rate limit enforcement (SQL window query)
    DELETE /files/{id} (message deletion in Telegram + DB row)
```

---

## Patterns to Follow

### Pattern: Lazy TelegramPool with Health Check

Do not pre-connect all bots at startup — connect on first use. This avoids startup failures if a user's bot is misconfigured. Add a periodic health-check (`asyncio.create_task`) that calls `client.get_me()` every 5 minutes and reconnects dead sessions silently.

### Pattern: Chunk Position as Source of Truth

Never reassemble chunks based on insertion order or timestamp. Use `ORDER BY position ASC` on `file_chunks` exclusively. Position is set by the ChunkingService at split time and is immutable.

### Pattern: Same Bot for Download

At insert time of `file_chunk`, store `worker_id` (FK to `storage_workers`). At download time, join `file_chunks → storage_workers` to get `bot_token`. This ensures the correct Telethon client instance is used, as Telegram's MTProto ties `file_id` to the uploading bot session.

### Pattern: is_uploaded Guard

Any endpoint listing or serving files must `WHERE is_uploaded = TRUE`. Never expose partially-uploaded files. The background task sets this only after all `asyncio.gather` results return successfully.

---

## Anti-Patterns to Avoid

### Anti-Pattern: Shared Bot Across Users

**What:** Using a single bot_token for all users' uploads.
**Why bad:** Telegram rate limits apply per bot. All users compete for the same 30 files/second limit. One user's heavy upload starves others.
**Instead:** Each user brings their own bot(s). Rate limits are per-user, per-bot.

### Anti-Pattern: Downloading with a Different Bot

**What:** Assigning the download to any available bot instead of the uploading bot.
**Why bad:** Telegram's MTProto protocol binds `file_id` to the session that uploaded the file. Attempting download with a different bot returns `FILE_REFERENCE_EXPIRED` or `ACCESS_HASH_MISMATCH`.
**Instead:** `file_chunks.worker_id` is mandatory and must be respected at download time.

### Anti-Pattern: BufferedResponse for Large Files

**What:** Downloading all chunks into memory, then returning a full response.
**Why bad:** A 2 GB file requires 2 GB of server RAM per concurrent download.
**Instead:** `StreamingResponse` with an async generator that yields one chunk at a time (~20 MB peak RAM).

### Anti-Pattern: Telethon in a Thread Pool

**What:** Using `asyncio.run_in_executor` to call Telethon from sync code.
**Why bad:** Telethon maintains internal asyncio queues and tasks tied to one event loop. Running it in a thread pool executor creates a different loop context and causes race conditions or deadlocks.
**Instead:** Keep all Telethon calls within `async def` functions in the main FastAPI event loop.

### Anti-Pattern: SQLite Session Files per Bot

**What:** Letting Telethon create `{bot_token}.session` files on disk.
**Why bad:** Disk I/O in Docker container, session files accumulate, not suitable for stateless deployment.
**Instead:** `StringSession()` keeps session data in memory. If persistence is needed across restarts, store the session string in the DB (`storage_workers.session_string`).

---

## Scalability Considerations

| Concern | At current scale (personal/small team) | At 100+ users | At 1000+ users |
|---------|----------------------------------------|---------------|----------------|
| Telethon connections | One per bot_token, all in-process | Same, fine | Consider worker processes with connection affinity |
| DB connections | asyncpg pool (min=5, max=20) | Same | PgBouncer connection pooling |
| Upload throughput | Sequential within one request | Parallel via asyncio.gather per upload | Multiple uvicorn workers (but TelegramPool must be per-process) |
| Rate limit tracking | SQL `last_used_at` | SQL sufficient | Redis if sub-second precision needed |
| File size | 2 GB via MTProto, 20 MB chunks | Same | Same |

---

## Sources

- [Telethon asyncio guide](https://docs.telethon.dev/en/stable/concepts/asyncio.html) — multi-client patterns, event loop rules — HIGH confidence
- [Telethon TelegramClient docs v1.42.0](https://docs.telethon.dev/en/stable/modules/client.html) — connection_retries, bot_token auth — HIGH confidence
- [Telegram MTProto file upload/download spec](https://core.telegram.org/api/files) — chunk size rules, parallel upload queues — HIGH confidence
- [FastAPI StreamingResponse docs](https://fastapi.tiangolo.com/advanced/custom-response/) — async generator pattern — HIGH confidence
- [FastAPI BackgroundTasks docs](https://fastapi.tiangolo.com/tutorial/background-tasks/) — background task mechanics — HIGH confidence
- [FastAPI + BackgroundTask + StreamingResponse issue](https://github.com/fastapi/fastapi/discussions/11022) — conflict between background task and streaming — MEDIUM confidence
- [Telegram Login Widget verification](https://core.telegram.org/widgets/login) — HMAC-SHA256 flow — HIGH confidence
- [FastAPI Telegram auth example](https://github.com/dabarov/fastapi-telegram) — Python implementation reference — MEDIUM confidence
- [Pentaract reference implementation](https://github.com/Dominux/Pentaract) — file_chunks schema, is_uploaded pattern, SQL rate limiting — MEDIUM confidence (Rust source, patterns adapted to Python)
- [asyncpg lifespan pool pattern](https://daniel.feldroy.com/posts/2025-10-using-asyncpg-with-fastapi-and-air) — startup/shutdown pool management — HIGH confidence
