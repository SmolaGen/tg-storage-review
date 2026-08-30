---
phase: 01-foundation
plan: 02
subsystem: infra
tags: [fastapi, sqlalchemy, asyncpg, alembic, pydantic-settings, docker, uvicorn, telethon, cryptg]

# Dependency graph
requires: []
provides:
  - FastAPI app с lifespan context manager (точка входа для будущей инициализации DB pool и TelegramPool)
  - pydantic-settings Settings (database_url, tg_api_id, tg_api_hash, jwt_secret)
  - SQLAlchemy async engine + async_sessionmaker (expire_on_commit=False)
  - GET /health endpoint ({status: ok})
  - Dockerfile (python:3.12-slim, alembic upgrade head + uvicorn)
  - docker-compose.yml (api:8000 + db:5432 с healthcheck и condition: service_healthy)
  - pyproject.toml (asyncio_mode=auto, pytest config)
  - requirements.txt (все зависимости включая cryptg)
  - .env.example (шаблон env vars)
affects: [01-03-models-migrations, all-subsequent-phases]

# Tech tracking
tech-stack:
  added:
    - fastapi==0.135.3
    - uvicorn[standard]
    - sqlalchemy==2.0.49
    - asyncpg==0.31.0
    - alembic==1.18.4
    - pydantic-settings
    - telethon==1.42.0
    - cryptg==0.5.2
    - PyJWT==2.12.1
    - python-multipart
    - pytest + pytest-asyncio + httpx==0.28.1
  patterns:
    - FastAPI lifespan context manager (не deprecated @app.on_event)
    - pydantic BaseSettings с SettingsConfigDict(env_file=".env")
    - SQLAlchemy create_async_engine + async_sessionmaker(expire_on_commit=False)
    - Docker Compose depends_on с condition: service_healthy + pg_isready healthcheck
    - Структурированное логирование через logging.getLogger(__name__)

key-files:
  created:
    - app/main.py
    - app/config.py
    - app/database.py
    - app/api/health.py
    - app/__init__.py
    - app/api/__init__.py
    - Dockerfile
    - docker-compose.yml
    - pyproject.toml
    - requirements.txt
    - .env.example
  modified: []

key-decisions:
  - "lifespan context manager вместо @app.on_event — следует официальной рекомендации FastAPI 0.95+"
  - "python:3.12-slim (не alpine) — cryptg требует C-заголовки, slim их содержит"
  - "alembic upgrade head в CMD Dockerfile — миграции применяются автоматически при старте (идемпотентно)"
  - "async_sessionmaker с expire_on_commit=False — предотвращает MissingGreenlet ошибки в async контексте"
  - "app/database.py создан дополнительно к плану — main.py импортирует engine из него для lifespan dispose"

patterns-established:
  - "Pattern: lifespan — @asynccontextmanager async def lifespan(app: FastAPI): yield + await engine.dispose()"
  - "Pattern: settings — settings = Settings() синглтон импортируется из app.config"
  - "Pattern: db-engine — engine создаётся на уровне модуля в app.database (не в lifespan)"
  - "Pattern: healthcheck — pg_isready + condition: service_healthy в docker-compose"

requirements-completed: [INFR-01]

# Metrics
duration: 2min
completed: 2026-04-06
---

# Phase 01 Plan 02: FastAPI каркас с lifespan, pydantic-settings и Docker Compose топология

**FastAPI 0.135.3 с lifespan context manager, async SQLAlchemy engine, pydantic-settings конфигурация и Docker Compose с PostgreSQL healthcheck — рабочий каркас для docker-compose up**

## Performance

- **Duration:** 2 min
- **Started:** 2026-04-06T00:16:47Z
- **Completed:** 2026-04-06T00:18:51Z
- **Tasks:** 2
- **Files modified:** 11

## Accomplishments
- FastAPI app с lifespan context manager (точка входа для DB pool и TelegramPool в Phase 3+)
- pydantic-settings Settings с полной конфигурацией через env vars
- Docker Compose топология: api (port 8000) + db (port 5432) с pg_isready healthcheck и condition: service_healthy
- Все зависимости зафиксированы включая cryptg==0.5.2 для 3-10x ускорения Telegram загрузок

## Task Commits

1. **Task 1: pydantic-settings + FastAPI app с lifespan + health endpoint** - `2e3617f` (feat)
2. **Task 2: Dockerfile + docker-compose.yml с healthcheck** - `28eba9f` (feat)

## Files Created/Modified
- `app/main.py` - FastAPI app с lifespan (asynccontextmanager), include_router health
- `app/config.py` - Settings(BaseSettings) с database_url, tg_api_id, tg_api_hash, jwt_secret
- `app/database.py` - create_async_engine + async_sessionmaker(expire_on_commit=False) + get_session dep
- `app/api/health.py` - GET /health возвращает {"status": "ok"}
- `app/__init__.py` - пустой пакетный файл
- `app/api/__init__.py` - пустой пакетный файл
- `Dockerfile` - python:3.12-slim, COPY requirements.txt + pip install + COPY . + alembic upgrade head && uvicorn
- `docker-compose.yml` - db с pg_isready healthcheck + api с depends_on condition: service_healthy
- `pyproject.toml` - asyncio_mode="auto", asyncio_default_fixture_loop_scope="function", testpaths=["tests"]
- `requirements.txt` - все зависимости включая cryptg==0.5.2
- `.env.example` - шаблон: DATABASE_URL, TG_API_ID, TG_API_HASH, JWT_SECRET, POSTGRES_*

## Decisions Made
- lifespan context manager: следует официальной рекомендации FastAPI 0.95+ (@app.on_event deprecated)
- python:3.12-slim: cryptg требует C-компилятор/заголовки, slim их содержит в отличие от alpine
- alembic upgrade head в CMD: миграции применяются автоматически при каждом старте контейнера (идемпотентно)
- expire_on_commit=False: предотвращает MissingGreenlet ошибки после commit в async контексте

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Создан app/database.py**
- **Found during:** Task 1 (создание app/main.py)
- **Issue:** plan указывал import engine из app.database в main.py для lifespan dispose, но app/database.py не был перечислен в files плана
- **Fix:** Создан app/database.py с create_async_engine, async_sessionmaker и get_session dependency
- **Files modified:** app/database.py (новый файл)
- **Verification:** python3 -c "from app.main import app" — успешно
- **Committed in:** 2e3617f (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (1 missing critical)
**Impact on plan:** Необходимый файл для корректной работы lifespan pattern. Нет scope creep.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- FastAPI каркас готов для Phase 1 Plan 3: SQLAlchemy models + Alembic migrations
- lifespan context manager готов принять инициализацию DB connection pool
- app/database.py готов для добавления target_metadata для Alembic env.py
- docker-compose.yml готов — PostgreSQL стартует с healthcheck перед API

## Self-Check: PASSED

---
*Phase: 01-foundation*
*Completed: 2026-04-06*
