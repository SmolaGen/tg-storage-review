# Phase 1: Foundation - Research

**Researched:** 2026-04-06
**Domain:** FastAPI + SQLAlchemy 2.0 async + Alembic + Docker Compose (greenfield infrastructure)
**Confidence:** HIGH

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

All implementation choices are at Claude's discretion — pure infrastructure phase.

Key constraints from research:
- `file_chunks` MUST have: `position INT NOT NULL`, `worker_id UUID NOT NULL`, `tg_file_id TEXT NOT NULL`, `message_id BIGINT NOT NULL` — retrofitting these later requires full rewrite
- `storage_workers` MUST have: `session_string TEXT` for StringSession (NOT SQLite file sessions)
- Use SQLAlchemy 2.0 async + asyncpg (NOT sync SQLAlchemy)
- Alembic with async template (`alembic init -t async`)
- FastAPI with lifespan context manager for DB pool and TelegramPool init
- Docker Compose: FastAPI (port 8000) + PostgreSQL (5432)
- Add `cryptg` to requirements from day one (3-10x Telegram upload speed)

### Claude's Discretion

All implementation choices (project layout, file naming, Alembic config details, Docker Compose details) are at Claude's discretion within the constraints above.

### Deferred Ideas (OUT OF SCOPE)

None — discussion stayed within phase scope.
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| INFR-01 | Сервис запускается через `docker-compose up` (FastAPI + PostgreSQL) | Docker Compose topology pattern with healthcheck documented; FastAPI lifespan pattern verified |
| INFR-02 | Telethon сессии хранятся как `StringSession` в PostgreSQL (не SQLite-файлы) | `session_string TEXT` column in `storage_workers`; StringSession in-memory pattern from ARCHITECTURE.md |
</phase_requirements>

---

## Summary

Phase 1 is a pure infrastructure phase for a greenfield Python project. The stack is fully decided: FastAPI 0.135.3, SQLAlchemy 2.0 async, asyncpg 0.31.0, Alembic 1.18.4, pydantic-settings, PostgreSQL 17. No business logic is implemented — only the project skeleton, 5-table DB schema, Alembic migrations, and Docker Compose topology.

All critical schema decisions are already locked by prior research (see ARCHITECTURE.md): `file_chunks` must include `position`, `worker_id`, `tg_file_id`, `message_id` from day one because retrofitting them later requires a full migration rewrite. The `storage_workers` table must have a `session_string TEXT` column to support Telethon StringSession persistence across restarts.

The planner can produce two plan waves: Wave 1 — project scaffolding (Dockerfile, docker-compose, pyproject.toml, settings), Wave 2 — SQLAlchemy models and Alembic migrations.

**Primary recommendation:** Use `alembic init -t async` (not default template) and `async_sessionmaker` with `expire_on_commit=False`. Lifespan context manager (not deprecated `@app.on_event`). All five tables must be defined in the initial migration — never retrofitted.

---

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| fastapi | 0.135.3 | HTTP API framework | Async-first, native lifespan API, StreamingResponse, Pydantic v2 built-in |
| uvicorn[standard] | latest | ASGI server | Official FastAPI runner; `[standard]` adds uvloop for performance |
| sqlalchemy | 2.0.49 | ORM + async engine | `create_async_engine`, `async_sessionmaker`, Alembic integration |
| asyncpg | 0.31.0 | PostgreSQL async driver | Required backend for SQLAlchemy async with PostgreSQL |
| alembic | 1.18.4 | DB migrations | Official SQLAlchemy migration tool; async template available |
| pydantic-settings | 2.x | Configuration via env vars | Built into Pydantic v2 ecosystem; `BaseSettings` with `.env` support |
| telethon | 1.42.0 | Telegram MTProto client | StringSession init in lifespan even if no workers exist yet |
| cryptg | 0.5.2 | Telethon AES acceleration | Must be installed from day one; Telethon auto-detects it |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| python-multipart | 0.0.x | Multipart file upload support | Required for FastAPI `UploadFile` |
| PyJWT | 2.12.1 | JWT tokens (used Phase 2+) | Add to requirements.txt now; actual use deferred |
| pytest | 8.x | Test runner | Phase 1 tests: health check, migration idempotency |
| pytest-asyncio | 1.3.0 | Async test support | Required for `async def` test functions |
| httpx | 0.28.1 | Async HTTP client for tests | FastAPI `AsyncClient` for integration tests |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| asyncpg (via SQLAlchemy) | psycopg3 | psycopg3 has nicer row factories but asyncpg is faster and better supported by SQLAlchemy 2.0 |
| pydantic-settings | python-dotenv | pydantic-settings adds type validation and model structure; no reason to use plain dotenv |
| uvicorn | gunicorn+uvicorn | gunicorn only needed for multi-worker; Phase 1 is single-process |

**Installation:**
```bash
pip install fastapi==0.135.3 "uvicorn[standard]"
pip install sqlalchemy==2.0.49 asyncpg==0.31.0 alembic==1.18.4
pip install pydantic-settings
pip install telethon==1.42.0 cryptg==0.5.2
pip install PyJWT==2.12.1 python-multipart
pip install pytest pytest-asyncio==1.3.0 httpx==0.28.1
```

---

## Architecture Patterns

### Recommended Project Structure
```
tg-storage/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app + lifespan
│   ├── config.py            # pydantic-settings Settings class
│   ├── database.py          # engine, async_session factory, get_session dep
│   ├── models/
│   │   ├── __init__.py
│   │   ├── base.py          # Base = declarative_base()
│   │   ├── user.py
│   │   ├── storage.py
│   │   ├── file.py
│   │   └── file_chunk.py
│   ├── api/
│   │   ├── __init__.py
│   │   └── health.py        # GET /health
│   └── telegram/
│       └── pool.py          # TelegramPool skeleton (Phase 3 fills it)
├── alembic/
│   ├── env.py               # async template
│   ├── script.py.mako
│   └── versions/
│       └── 0001_initial_schema.py
├── tests/
│   ├── conftest.py
│   └── test_health.py
├── alembic.ini
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

### Pattern 1: FastAPI Lifespan Context Manager
**What:** Replaces deprecated `@app.on_event("startup")` and `@app.on_event("shutdown")`.
**When to use:** Always — the old `on_event` API is deprecated since FastAPI 0.95.0.
**Example:**
```python
# Source: https://fastapi.tiangolo.com/advanced/events/
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: init DB pool, TelegramPool
    engine = create_async_engine(settings.database_url)
    app.state.engine = engine
    app.state.telegram_pool = TelegramPool()
    yield
    # Shutdown: disconnect all Telegram clients, dispose DB engine
    await app.state.telegram_pool.shutdown()
    await engine.dispose()

app = FastAPI(lifespan=lifespan)
```

### Pattern 2: SQLAlchemy 2.0 Async Session Factory
**What:** `create_async_engine` + `async_sessionmaker` + dependency injection per request.
**When to use:** Every database operation in a FastAPI route handler.
**Example:**
```python
# Source: https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

engine = create_async_engine(
    settings.database_url,  # "postgresql+asyncpg://user:pass@host/db"
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,
)

async_session = async_sessionmaker(engine, expire_on_commit=False)

async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
```

### Pattern 3: Alembic Async Migrations
**What:** Async Alembic env.py that uses `async_engine_from_config` + `connection.run_sync`.
**When to use:** Every project using SQLAlchemy async with Alembic.
**Setup:**
```bash
# Source: Alembic official docs
alembic init -t async alembic
```
The generated `env.py` uses:
```python
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

connectable = async_engine_from_config(
    config.get_section(config.config_ini_section, {}),
    prefix="sqlalchemy.",
    poolclass=NullPool,
)
async with connectable.connect() as connection:
    await connection.run_sync(do_run_migrations)
await connectable.dispose()
```
Key: `NullPool` is used because Alembic creates its own connection — no pooling needed for migrations.

### Pattern 4: pydantic-settings Configuration
**What:** `BaseSettings` subclass reads from env vars and `.env` file with type validation.
**When to use:** All configuration — never hardcode values.
**Example:**
```python
# Source: https://docs.pydantic.dev/latest/concepts/pydantic_settings/
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str
    tg_api_id: int
    tg_api_hash: str
    jwt_secret: str
    postgres_db: str = "tgstorage"

settings = Settings()
```

### Pattern 5: Docker Compose Healthcheck Wait
**What:** `depends_on` with `condition: service_healthy` ensures FastAPI does not start before PostgreSQL is ready.
**When to use:** Always — without it, the app container starts before the DB is accepting connections, causing connection errors on startup.
**Example:**
```yaml
services:
  db:
    image: postgres:17-alpine
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U tgstorage"]
      interval: 5s
      timeout: 5s
      retries: 5

  api:
    build: .
    depends_on:
      db:
        condition: service_healthy
```

### Anti-Patterns to Avoid
- **`@app.on_event("startup")`:** Deprecated since FastAPI 0.95.0. Use `lifespan` context manager.
- **`expire_on_commit=True` (default):** Causes implicit lazy-load IO after commits in async context. Always set `expire_on_commit=False`.
- **Sync SQLAlchemy in async FastAPI:** Using `create_engine` (not `create_async_engine`) blocks the event loop. Symptom: application hangs under load.
- **SQLite session files for Telethon:** Creates disk state, not container-safe. Use `StringSession()` and store the string in DB.
- **Alembic default template with async engine:** Default `env.py` is sync. Must use `-t async` flag or migrations will fail with asyncpg.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Env var configuration | Custom os.environ parsing | pydantic-settings BaseSettings | Type validation, nested config, .env support, error messages |
| DB session lifecycle | Manual session open/close in routes | `async_sessionmaker` + `Depends(get_session)` | Exception-safe, guaranteed cleanup |
| DB migration versioning | Manual SQL scripts | Alembic | Autogenerate, history, rollback, idempotency |
| DB pool management | Manual asyncpg pool | SQLAlchemy `create_async_engine` pool | Retry, overflow, timeout, recycle all handled |
| Startup/shutdown hooks | try/finally in main | FastAPI `lifespan` | Clean, testable, works with testing tools |

**Key insight:** Every "simple" infrastructure concern has proven edge cases (pool exhaustion, session leak, migration conflicts). The standard tools handle them correctly.

---

## Common Pitfalls

### Pitfall 1: Alembic Cannot Find Model Metadata
**What goes wrong:** `alembic revision --autogenerate` generates empty migration with no detected changes.
**Why it happens:** `env.py` has `target_metadata = None` instead of pointing to your `Base.metadata`.
**How to avoid:** In `env.py` add `from app.models.base import Base; target_metadata = Base.metadata` before the migration functions.
**Warning signs:** Generated migration file has empty `upgrade()` and `downgrade()` functions.

### Pitfall 2: Alembic Cannot Connect to DB During Migration
**What goes wrong:** `alembic upgrade head` fails with connection error when run inside Docker.
**Why it happens:** `alembic.ini` `sqlalchemy.url` is a hardcoded string and doesn't read from env vars.
**How to avoid:** Override URL in `env.py` before creating the engine: `config.set_main_option("sqlalchemy.url", settings.database_url)`.
**Warning signs:** Migration works locally but fails in Docker.

### Pitfall 3: `expire_on_commit` Causes MissingGreenlet Error
**What goes wrong:** Accessing a model attribute after commit raises `MissingGreenlet: greenlet_spawn has not been called`.
**Why it happens:** Default `expire_on_commit=True` marks all attributes as expired after commit, triggering implicit IO.
**How to avoid:** `async_sessionmaker(engine, expire_on_commit=False)`.
**Warning signs:** Error only appears after the first commit, not during inserts.

### Pitfall 4: FastAPI Container Starts Before PostgreSQL Is Ready
**What goes wrong:** `docker-compose up` succeeds but the API crashes immediately with `asyncpg.exceptions.ConnectionRefusedError`.
**Why it happens:** `depends_on: [db]` only waits for the container to start, not for PostgreSQL to accept connections.
**How to avoid:** Use `depends_on: db: condition: service_healthy` with a healthcheck on the db service.
**Warning signs:** Works on second `docker-compose up`, fails on first (cold start).

### Pitfall 5: Alembic Idempotency Violation
**What goes wrong:** Running `alembic upgrade head` twice raises `DuplicateTableError` or similar.
**Why it happens:** Migration does not use conditional DDL (`IF NOT EXISTS`) and Alembic's version tracking is corrupted.
**How to avoid:** Keep Alembic version table intact; never run raw `CREATE TABLE` outside migrations. Running `upgrade head` twice is safe as long as the `alembic_version` table exists and tracks applied revisions correctly — Alembic skips already-applied migrations automatically.
**Warning signs:** Migration fails on a non-empty database.

### Pitfall 6: StringSession Not Persisted
**What goes wrong:** Every service restart loses all bot sessions; bots must re-authenticate on every startup.
**Why it happens:** `StringSession()` is in-memory only. If not serialized and stored in DB, session string is lost on container restart.
**How to avoid:** After `client.start(bot_token=token)`, call `client.session.save()` to get the session string, store in `storage_workers.session_string`. On next startup, pass stored string to `StringSession(stored_string)`.
**Warning signs:** Telegram re-authentication required after every restart; `FloodWaitError` on repeated auth attempts.

---

## Code Examples

Verified patterns from official sources:

### SQLAlchemy Models with UUID PK
```python
# Source: SQLAlchemy 2.0 official docs
import uuid
from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram user_id
    username: Mapped[str | None] = mapped_column(Text)
    first_name: Mapped[str] = mapped_column(Text, nullable=False)
    last_name: Mapped[str | None] = mapped_column(Text)
    photo_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

class FileChunk(Base):
    __tablename__ = "file_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    worker_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("storage_workers.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    tg_file_id: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (UniqueConstraint("file_id", "position"),)
```

### Storage Workers with session_string
```python
class StorageWorker(Base):
    __tablename__ = "storage_workers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    storage_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("storages.id", ondelete="CASCADE"), nullable=False)
    bot_token: Mapped[str] = mapped_column(Text, nullable=False)
    session_string: Mapped[str | None] = mapped_column(Text)  # INFR-02: StringSession persistence
    last_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("storage_id", "bot_token"),)
```

### pytest-asyncio pyproject.toml configuration
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"

[tool.coverage.run]
source = ["app"]
omit = ["tests/*"]
```

### Health Check Endpoint
```python
# Source: FastAPI official docs
from fastapi import APIRouter

router = APIRouter()

@router.get("/health")
async def health_check():
    return {"status": "ok"}
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `@app.on_event("startup")` | `lifespan` context manager | FastAPI 0.95.0 (2023) | Old approach deprecated, still works but not recommended |
| `sessionmaker` (sync) | `async_sessionmaker` | SQLAlchemy 2.0 (2023) | Required for async; `expire_on_commit=False` default recommended |
| `alembic init` (sync env) | `alembic init -t async` | Alembic 1.x (2022+) | Must use async template with asyncpg; sync env hangs |
| `python-jose` for JWT | `PyJWT` | FastAPI docs updated 2024-2025 | python-jose has CVEs and was abandoned for 4 years |
| `StringSession` SQLite file | `StringSession` in-memory + DB persist | Telethon best practice | Container-safe, concurrent-safe, no disk state |

**Deprecated/outdated:**
- `@app.on_event`: Works but deprecated, will be removed in a future FastAPI version
- Sync SQLAlchemy engine in async FastAPI: Blocks event loop, causes poor performance
- Default `alembic init` template: Doesn't work with asyncpg driver

---

## Open Questions

1. **`message_id` column naming in `file_chunks`**
   - What we know: ARCHITECTURE.md shows `tg_file_id TEXT NOT NULL` in the schema SQL
   - What's unclear: CONTEXT.md success criteria mentions `message_id BIGINT NOT NULL` as a separate column, but ARCHITECTURE.md schema SQL shows only `tg_file_id TEXT`. REQUIREMENTS.md UPLD-03 says "Каждый чанк сохраняется в `file_chunks` с `position`, `worker_id`, `tg_file_id`, `message_id`" — suggesting BOTH columns exist
   - Recommendation: Include BOTH `tg_file_id TEXT NOT NULL` AND `message_id BIGINT NOT NULL` in `file_chunks`. `tg_file_id` is the Telegram internal file reference; `message_id` is the channel message ID needed for `FILE_REFERENCE_EXPIRED` recovery (Phase 5, DWNL-03).

2. **PostgreSQL version: 16 vs 17**
   - What we know: ARCHITECTURE.md Docker Compose snippet uses `postgres:16-alpine`; STACK.md recommends PostgreSQL 17
   - What's unclear: Minor version difference, both support required features
   - Recommendation: Use `postgres:17-alpine` to match STACK.md recommendation. Both use `gen_random_uuid()` which is available in both.

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.x + pytest-asyncio 1.3.0 |
| Config file | `pyproject.toml` — `[tool.pytest.ini_options]` (Wave 0 creates it) |
| Quick run command | `pytest tests/ -x -q` |
| Full suite command | `pytest tests/ -v --tb=short` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| INFR-01 | `GET /health` returns `{"status": "ok"}` with 200 | integration | `pytest tests/test_health.py -x` | ❌ Wave 0 |
| INFR-01 | All 5 tables exist in DB after `alembic upgrade head` | integration | `pytest tests/test_migrations.py -x` | ❌ Wave 0 |
| INFR-01 | `alembic upgrade head` is idempotent (run twice, no error) | integration | `pytest tests/test_migrations.py::test_idempotent -x` | ❌ Wave 0 |
| INFR-02 | `storage_workers` has `session_string TEXT` column | integration | `pytest tests/test_migrations.py::test_schema_columns -x` | ❌ Wave 0 |
| INFR-02 | `file_chunks` has `position`, `worker_id`, `tg_file_id`, `message_id` | integration | `pytest tests/test_migrations.py::test_schema_columns -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `pytest tests/ -x -q`
- **Per wave merge:** `pytest tests/ -v --tb=short`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/conftest.py` — async engine fixture pointing to test DB, `alembic upgrade head` fixture
- [ ] `tests/test_health.py` — covers INFR-01 health endpoint
- [ ] `tests/test_migrations.py` — covers INFR-01 (5 tables exist, idempotency) + INFR-02 (columns)
- [ ] `pyproject.toml` — `asyncio_mode = "auto"` + pytest config
- [ ] Framework install: `pip install pytest pytest-asyncio==1.3.0 httpx==0.28.1`

---

## Sources

### Primary (HIGH confidence)
- [FastAPI Lifespan Events docs](https://fastapi.tiangolo.com/advanced/events/) — lifespan context manager pattern, exact code
- [SQLAlchemy asyncio docs](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html) — `create_async_engine`, `async_sessionmaker`, session per request
- [Alembic async template (GitHub)](https://github.com/sqlalchemy/alembic/blob/main/alembic/templates/async/env.py) — `async_engine_from_config`, `NullPool`, `run_sync`
- [Pydantic Settings docs](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) — `BaseSettings`, `SettingsConfigDict`, env_file
- [FastAPI Settings docs](https://fastapi.tiangolo.com/advanced/settings/) — pydantic-settings integration pattern
- STACK.md (prior research) — all package versions verified against PyPI
- ARCHITECTURE.md (prior research) — PostgreSQL schema, Docker Compose topology, TelegramPool design

### Secondary (MEDIUM confidence)
- [Alembic cookbook](https://alembic.sqlalchemy.org/en/latest/cookbook.html) — async migration patterns
- [FastAPI async tests docs](https://fastapi.tiangolo.com/advanced/async-tests/) — httpx AsyncClient pattern
- [pytest-asyncio PyPI](https://pypi.org/project/pytest-asyncio/) — `asyncio_mode = "auto"` configuration

### Tertiary (LOW confidence)
- [Leapcell blog: SQLAlchemy 2.0 + asyncpg](https://leapcell.io/blog/building-high-performance-async-apis-with-fastapi-sqlalchemy-2-0-and-asyncpg) — pool_size/max_overflow recommendations (not from official docs)

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all versions verified against PyPI in prior STACK.md research
- Architecture: HIGH — patterns verified against official FastAPI, SQLAlchemy, Alembic docs
- Pitfalls: HIGH — pitfalls 1-5 verified against official docs; pitfall 6 (StringSession) MEDIUM (Telethon docs + architecture research)

**Research date:** 2026-04-06
**Valid until:** 2026-05-06 (stable ecosystem, 30-day window)
