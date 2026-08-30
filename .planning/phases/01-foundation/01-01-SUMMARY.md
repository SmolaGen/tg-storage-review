---
phase: 01-foundation
plan: "01"
subsystem: database
tags: [sqlalchemy, asyncpg, alembic, postgresql, pydantic-settings]

requires: []
provides:
  - "5 таблиц PostgreSQL: users, storages, storage_workers, files, file_chunks"
  - "SQLAlchemy 2.0 async модели с UUID PK и BigInteger для Telegram ID"
  - "Alembic async миграции (async_engine_from_config + NullPool)"
  - "async engine factory (database.py) с expire_on_commit=False"
  - "Начальная миграция 0001_initial_schema с полным downgrade"
affects: [02-auth, 03-telegram-pool, 04-upload, 05-download]

tech-stack:
  added:
    - sqlalchemy==2.0.49 (async ORM)
    - asyncpg==0.31.0 (PostgreSQL async driver)
    - alembic==1.18.4 (async migrations)
    - pydantic-settings (Settings с env_file)
  patterns:
    - "DeclarativeBase + Mapped[T] + mapped_column() — SQLAlchemy 2.0 style"
    - "async_sessionmaker(engine, expire_on_commit=False) — предотвращает MissingGreenlet"
    - "async_engine_from_config + pool.NullPool — async Alembic pattern"
    - "config.set_main_option('sqlalchemy.url', settings.database_url) — Pitfall #2 fix"

key-files:
  created:
    - app/models/base.py
    - app/models/user.py
    - app/models/storage.py
    - app/models/storage_worker.py
    - app/models/file.py
    - app/models/file_chunk.py
    - app/models/__init__.py
    - alembic.ini
    - alembic/env.py
    - alembic/script.py.mako
    - alembic/versions/0001_initial_schema.py
  modified:
    - app/database.py (дополнен async engine factory)

key-decisions:
  - "file_chunks содержит position INT, worker_id UUID, tg_file_id TEXT, message_id BIGINT — все NOT NULL с первой миграции (ретрофитинг требует полного переписывания)"
  - "storage_workers.session_string TEXT nullable — StringSession хранится в PostgreSQL, не SQLite-файлы (INFR-02)"
  - "alembic/env.py переопределяет sqlalchemy.url из pydantic-settings — обеспечивает корректную работу в Docker"
  - "expire_on_commit=False предотвращает MissingGreenlet при доступе к атрибутам после commit в async контексте"

patterns-established:
  - "Pattern: SQLAlchemy модели — UUID(as_uuid=True) для PK хранилищ, BigInteger для Telegram ID"
  - "Pattern: Alembic async — всегда async_engine_from_config + NullPool в env.py"
  - "Pattern: FK порядок в миграции — users → storages → storage_workers → files → file_chunks"

requirements-completed: [INFR-01, INFR-02]

duration: 6min
completed: "2026-04-06"
---

# Phase 01: Foundation — Plan 01 Summary

**SQLAlchemy 2.0 async модели (5 таблиц) + Alembic async миграции с UUID PK, BigInteger Telegram ID, StringSession persistence**

## Performance

- **Duration:** ~6 min
- **Started:** 2026-04-06T00:16:44Z
- **Completed:** 2026-04-06T00:22:54Z
- **Tasks:** 2
- **Files modified:** 12

## Accomplishments

- 5 SQLAlchemy 2.0 моделей с точными типами колонок (UUID, BigInteger, TIMESTAMP с timezone)
- Alembic async env.py с async_engine_from_config + NullPool + Pitfall #2 fix (URL из settings)
- Начальная миграция 0001 создаёт все 5 таблиц в правильном FK-порядке с полным downgrade
- file_chunks содержит все критичные колонки: position, worker_id, tg_file_id, message_id — все NOT NULL
- storage_workers.session_string TEXT nullable — готов к Telethon StringSession (INFR-02)

## Task Commits

Каждый task зафиксирован атомарно:

1. **Task 1: SQLAlchemy 2.0 async модели + database.py** — `61faf76` (feat)
2. **Task 2: Alembic async конфигурация + начальная миграция** — `d00dd04` (feat)

## Files Created/Modified

- `app/models/base.py` — DeclarativeBase для всех моделей
- `app/models/user.py` — User модель (Telegram user_id = BigInteger PK)
- `app/models/storage.py` — Storage модель с UUID PK и FK на users
- `app/models/storage_worker.py` — StorageWorker с session_string TEXT nullable
- `app/models/file.py` — File модель с is_uploaded, is_deleted (soft delete)
- `app/models/file_chunk.py` — FileChunk с position, worker_id, tg_file_id, message_id
- `app/models/__init__.py` — реэкспорт всех моделей для Alembic autogenerate
- `app/database.py` — async engine + async_session factory + get_session dependency
- `alembic.ini` — конфигурация Alembic с script_location и prepend_sys_path
- `alembic/env.py` — async шаблон с async_engine_from_config + NullPool + URL override
- `alembic/script.py.mako` — шаблон для генерации новых миграций
- `alembic/versions/0001_initial_schema.py` — начальная миграция всех 5 таблиц

## Decisions Made

- Использован SQLAlchemy 2.0 style (Mapped[T] + mapped_column) вместо legacy Column
- alembic/env.py читает DATABASE_URL из pydantic-settings — работает и локально, и в Docker
- Начальная миграция написана вручную (не autogenerate) для гарантии точной схемы

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Создан app/config.py до начала Task 1**
- **Found during:** Task 1 (до создания моделей)
- **Issue:** app/database.py импортирует `from app.config import settings`, но config.py не был в списке файлов Task 1
- **Fix:** Создан app/config.py с pydantic-settings BaseSettings до создания database.py
- **Files modified:** app/config.py
- **Verification:** `from app.config import settings` импортируется без ошибок
- **Committed in:** 61faf76 (часть Task 1 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Необходимое условие для корректной работы database.py. Нет scope creep.

## Issues Encountered

Часть файлов (app/__init__.py, app/config.py, app/database.py) уже была зафиксирована в предыдущих коммитах (FastAPI каркас был создан в Plan 02 до Plan 01). Это нормально — модели добавлены корректно поверх существующего каркаса.

## User Setup Required

None — no external service configuration required for this plan. PostgreSQL запускается через docker-compose (Plan 02).

## Next Phase Readiness

- Все 5 таблиц определены в моделях и в миграции — готово для Plan 02 (docker-compose + тесты)
- `get_session` dependency готова для использования в API роутах Phase 2+
- Alembic upgrade head идемпотентен — двойной вызов не даёт ошибок (Alembic version table)
- Критичные колонки (position, worker_id, tg_file_id, message_id) зафиксированы с Phase 1 — ретрофитинг не потребуется

---
*Phase: 01-foundation*
*Completed: 2026-04-06*
