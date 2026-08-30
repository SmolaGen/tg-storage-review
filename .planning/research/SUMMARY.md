# Project Research Summary

**Project:** tg-storage
**Domain:** Telegram-backed multi-tenant file storage service (REST API + Bot interface)
**Researched:** 2026-04-06
**Confidence:** HIGH

## Executive Summary

tg-storage — это API-first файловое хранилище, где Telegram выступает бесплатным бэкендом неограниченного объёма. Ближайший аналог — Pentaract (Rust, 1.7k stars). Ключевое отличие от стандартных хранилищ: файлы режутся на 20 МБ чанки и параллельно загружаются через MTProto (Telethon) несколькими ботами пользователя. file_id в Telegram жёстко привязан к боту-загрузчику — архитектура вынуждена хранить `worker_id` per chunk и при скачивании всегда использовать тот же бот. Скачивание реализуется через `StreamingResponse` с async-генератором — без буферизации на сервере.

Рекомендованный стек полностью совпадает с профилем разработчика: FastAPI 0.135.3, SQLAlchemy 2.0 + asyncpg, PostgreSQL 17, Alembic. Из специфичных зависимостей: Telethon 1.42.0 (единственный зрелый Python MTProto-клиент, 2 ГБ лимит vs 50 МБ у HTTP Bot API) и cryptg 0.5.2 (3-10x ускорение AES-IGE без изменений кода). Redis явно исключён из PROJECT.md — rate limiting через SQL rolling window (Pentaract-паттерн).

Главные риски сгруппированы в три категории: (1) схема БД — если с первого дня не заложить `position`, `worker_id`, `message_id` в `file_chunks`, потребуется переписывать download-пайплайн; (2) управление сессиями Telethon — SQLite-сессии нельзя шарить между процессами, нужен `StringSession` в памяти; (3) параллельная загрузка — unbounded `asyncio.gather` без семафора гарантированно триггерит `FloodWaitError`. Все три риска устраняются на этапе проектирования схемы и фазы Foundation.

---

## Key Findings

### Recommended Stack

Стек полностью верифицирован через PyPI с точными версиями. Telethon 1.42.0 — единственный выбор для MTProto в Python: Pyrogram сопоставим по возможностям, но менее проверен для файловых операций; Telethon 2.0 — альфа с нестабильным API, пинить строго `==1.42.0`. HTTP Bot API (aiogram, python-telegram-bot) не рассматривается — 50 МБ лимит несовместим с продуктом.

**Основные технологии:**
- FastAPI 0.135.3 — HTTP API, `StreamingResponse` для стриминга, нативный async
- Telethon 1.42.0 — MTProto-клиент, bot_token-режим, 2 ГБ лимит файлов
- cryptg 0.5.2 — обязательно: 3-10x ускорение загрузки, Telethon подхватывает автоматически
- SQLAlchemy 2.0.49 + asyncpg 0.31.0 — async ORM, 5x быстрее psycopg2
- PostgreSQL 17 — основная БД + SQL rolling window rate limiting (без Redis)
- Alembic 1.18.4 — миграции (`alembic init -t async` для async-шаблона)
- PyJWT 2.12.1 — JWT (python-jose исключён: заброшен, CVE-уязвимости)
- pytest-asyncio 1.3.0 + httpx 0.28.1 — async-тесты и интеграционные тесты

**Что не использовать:** python-jose, passlib, Telethon 2.0.0a0, Redis, Celery/Dramatiq.

### Expected Features

**Обязательные (table stakes) — MVP:**
- Загрузка файла через `POST /files` (multipart) с `is_uploaded` флагом
- Скачивание через `GET /files/{id}/content` (StreamingResponse, без буферизации)
- Список файлов с метаданными (name, size, mime, created_at, is_uploaded)
- Удаление файла (только DB-запись, Telegram не поддерживает удаление через бота)
- Аутентификация через Telegram Login Widget + JWT (access 15 мин, refresh 30 дней)
- Регистрация bot_token + chat_id (хранилище пользователя)
- Корректные HTTP-статусы: 404, 413, 422, 429, 500

**Конкурентные преимущества (differentiators):**
- Параллельная загрузка чанков через `asyncio.gather` по нескольким ботам — не просто фича, а требование корректности (лимит 20 req/30s на бот)
- StreamingResponse без серверной буферизации — минимальный RAM footprint
- MTProto вместо HTTP Bot API — 2 ГБ на файл
- Telegram Bot-интерфейс: пересылка файла боту напрямую (без REST-клиента)
- Опрос статуса загрузки через `GET /files/{id}` (polling на `is_uploaded`)

**Отложить (v2+):**
- Папки/иерархия (достаточно flat list с prefix-фильтром)
- Resumable upload (tus/GCS-стиль) — не оправдывает сложности для v1
- Дедупликация по content hash — ломает streaming-upload
- Share-ссылки с TTL
- Переименование файлов

**Явные anti-features (не строить никогда в v1):**
- S3-совместимый API — PROJECT.md исключил явно
- Web UI / dashboard — API-first сервис
- OAuth/email — только Telegram Login
- Thumbnail/transcoding

### Architecture Approach

Архитектура — классический layered FastAPI с ключевым компонентом `TelegramPool` (dict `{bot_token: TelegramClient}`), который управляет жизненным циклом MTProto-сессий. Pool инициализируется в `lifespan`, клиенты создаются лениво при первом использовании, `StringSession` хранится в памяти. FileService оркестрирует chunking + параллельную загрузку через `asyncio.gather` с семафором на бот. Download — последовательный async-генератор (порядок байт обязателен), передаётся в `StreamingResponse`.

**Основные компоненты:**
1. **Auth Router + AuthService** — Telegram Login Widget HMAC-SHA256 верификация, JWT issue/refresh/revoke
2. **Files Router + FileService** — upload pipeline (202 + polling), download StreamingResponse, list, delete
3. **Storages Router + StorageService** — CRUD хранилищ, регистрация/валидация bot_token через `get_me()`
4. **TelegramPool** — dict `{bot_token: TelegramClient}`, lazy init, graceful shutdown, reconnect
5. **ChunkingService** — stateless: split bytes → 20 МБ chunks, round-robin worker assignment
6. **PostgreSQL** — 5 таблиц: `users`, `storages`, `storage_workers`, `files`, `file_chunks`

**Критические решения схемы БД:**
- `file_chunks.position` — NOT NULL, всегда `ORDER BY position ASC`, никогда по `created_at`
- `file_chunks.worker_id` — NOT NULL FK → `storage_workers`, обязателен для download routing
- `file_chunks.tg_file_id` — Telegram message_id для refresh FILE_REFERENCE_EXPIRED
- `files.is_uploaded` — защита от частично загруженных файлов в листинге

### Critical Pitfalls

1. **SQLite-сессия Telethon блокируется под конкурентностью** — использовать `StringSession()` in-memory, один `TelegramClient` на bot_token, создать в `lifespan`. При горизонтальном масштабировании — хранить session_string в БД.

2. **file_id привязан к боту-загрузчику** — `file_chunks.worker_id` NOT NULL обязателен с первого дня. Download должен джойнить `file_chunks → storage_workers` для получения `bot_token`. Ротация токена = потеря всех файлов этого бота.

3. **FILE_REFERENCE_EXPIRED ломает скачивание без предупреждения** — хранить `message_id` в `file_chunks`. При ошибке: `client.get_messages(chat_id, ids=message_id)` → свежая ссылка → retry. Реализовать как прозрачный декоратор.

4. **Unbounded asyncio.gather триггерит FloodWaitError** — `asyncio.Semaphore(3)` per bot, явный catch `FloodWaitError` с `await asyncio.sleep(error.seconds + 5)`, `flood_sleep_threshold=300` на клиенте.

5. **Zombie-записи от незавершённых загрузок** — background job: помечать `is_uploaded=FAILED` если `is_uploaded=FALSE` более 30 мин. Атомарный rollback при failure: удалить chunk-записи в транзакции.

---

## Implications for Roadmap

Порядок фаз диктуется зависимостями: схема БД → auth → telegram pool → upload → download → bot → hardening. Нельзя начинать upload до TelegramPool; нельзя начинать download до upload (нет `file_chunks` строк).

### Phase 1: Foundation
**Rationale:** Всё остальное зависит от схемы БД и базовой инфраструктуры. Pitfalls 1, 2, 3, 11 требуют правильных колонок с первого дня — `position`, `worker_id`, `message_id` — иначе переписывать download.
**Delivers:** PostgreSQL схема (5 таблиц), Alembic async миграции, asyncpg pool в lifespan, pydantic-settings конфигурация, Docker Compose (api + db), Ruff + mypy setup.
**Addresses:** Базовая инфраструктура
**Avoids:** Pitfall 1 (StringSession vs SQLite), Pitfall 2 (worker_id в схеме), Pitfall 3 (message_id в схеме), Pitfall 11 (position колонка)

### Phase 2: Auth
**Rationale:** JWT middleware разблокирует все защищённые эндпоинты. Без него невозможно тестировать Files/Storages API.
**Delivers:** Telegram Login Widget callback, HMAC-SHA256 верификация без сторонних библиотек, JWT issue/refresh/revoke (PyJWT), Bearer middleware, `refresh_tokens` таблица с ротацией.
**Uses:** PyJWT 2.12.1, стандартная библиотека `hmac` + `hashlib`
**Implements:** AuthService, Auth Router

### Phase 3: TelegramPool + StorageService
**Rationale:** FileService требует инициализированный pool и зарегистрированных workers. Валидация bot_token на этапе регистрации предотвращает Pitfall 10 (невалидные токены падают при первой загрузке, а не при регистрации).
**Delivers:** TelegramPool (lazy init, StringSession, graceful shutdown, reconnect), CRUD storages, регистрация/удаление workers с валидацией через `get_me()`, проверка прав бота в канале.
**Uses:** Telethon 1.42.0, cryptg 0.5.2
**Avoids:** Pitfall 10 (валидация токена при регистрации), Pitfall 7 (StringSession вместо SQLite)

### Phase 4: Upload Pipeline
**Rationale:** Зависит от Phase 3 (нужны workers). Центральная фича продукта. 202 + polling паттерн предотвращает HTTP timeout для больших файлов.
**Delivers:** ChunkingService (split 20 МБ), round-robin worker assignment с `last_used_at`, `asyncio.gather` + `Semaphore(3)` per bot, `FloodWaitError` handling с backoff, `POST /files` (202 Accepted + file_id), `GET /files/{id}` polling.
**Implements:** FileService.upload_to_telegram, batch INSERT file_chunks
**Avoids:** Pitfall 4 (Semaphore), Pitfall 5 (атомарный rollback), Pitfall 8 (batch insert vs N отдельных)

### Phase 5: Download Pipeline
**Rationale:** Зависит от Phase 4 (нужны `file_chunks` строки). Требует корректного routing через `worker_id` и retry на `FILE_REFERENCE_EXPIRED`.
**Delivers:** async generator `_stream_chunks`, `StreamingResponse` через Telethon `iter_download`, join `file_chunks → storage_workers` для bot routing, retry-декоратор на `FILE_REFERENCE_EXPIRED`, `GET /files/{id}/download`.
**Uses:** FastAPI `StreamingResponse`, Telethon `iter_download`
**Avoids:** Pitfall 2 (same bot для download), Pitfall 3 (message_id refresh), Pitfall 6 (iter_download vs bytes)

### Phase 6: Files CRUD + List
**Rationale:** После upload/download — простые эндпоинты, можно выделить в отдельную фазу или объединить с Phase 4/5.
**Delivers:** `GET /files` (list с пагинацией, только `is_uploaded=TRUE`), `DELETE /files/{id}` (DB-only, Telegram сообщения не удаляет), корректные MIME-type passthrough.
**Avoids:** Pitfall 5 (is_uploaded guard в листинге)

### Phase 7: Telegram Bot Interface
**Rationale:** Параллельный путь к Phase 4/5, не вносит новых зависимостей. Переиспользует FileService.upload() напрямую (не через HTTP).
**Delivers:** Bot webhook handler, приём forwarded-файлов, вызов FileService.ingest(), привязка к user account через JWT/session.
**Implements:** bot entrypoint в Docker Compose

### Phase 8: Hardening + Observability
**Rationale:** Производственная готовность. Background jobs, мониторинг RAM, алерты на FloodWaitError/throttling.
**Delivers:** Janitor job (zombie files cleanup, `is_uploaded=FAILED` после 30 мин), SQL rate limit enforcement, `/files/{id}/retry` эндпоинт, health-check для bot tokens (`client.get_me()` каждые 5 мин), мониторинг RSS Telethon-клиентов.
**Avoids:** Pitfall 5 (janitor), Pitfall 9 (RAM growth), Pitfall 12 (FLOOD_PREMIUM_WAIT мониторинг)

### Phase Ordering Rationale

- Phase 1 закладывает схему с обязательными колонками (`position`, `worker_id`, `message_id`) — исправлять их позже означает переписывать download-пайплайн полностью.
- Phase 2 раньше Phase 3 потому что JWT middleware нужен для тестирования Storages API.
- Phase 3 перед Phase 4 — FileService не может работать без инициализированного TelegramPool и зарегистрированных workers.
- Phase 4 перед Phase 5 — скачивать нечего без `file_chunks` строк.
- Phase 7 параллельна Phase 6 — нет взаимозависимости.
- Phase 8 всегда последняя — hardening поверх рабочей системы.

### Research Flags

Фазы, требующие углублённого исследования при планировании:
- **Phase 4 (Upload Pipeline):** Конкретная реализация `FloodWaitError` backoff + Semaphore tuning; точные лимиты Telegram могут варьироваться по "репутации" бота — проверить в реальном тесте.
- **Phase 5 (Download):** `iter_download` API в Telethon 1.42.0 — убедиться в совместимости с chunked `file_id` (не полным файлом); параметр `request_size` для оптимизации.
- **Phase 8 (Hardening):** SQL rolling window запрос для rate limiting — конкретный паттерн из Pentaract нужно адаптировать под asyncpg.

Фазы со стандартными паттернами (research-phase не нужен):
- **Phase 1:** PostgreSQL + Alembic async — хорошо задокументировано, стандартный стек.
- **Phase 2:** JWT + Telegram Login Widget HMAC — официальная документация Telegram, стандартная реализация.
- **Phase 6:** CRUD эндпоинты — стандартный FastAPI.

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | Все версии верифицированы через PyPI. Telethon 1.42.0 — текущий stable (Nov 2025). PyJWT — официальная рекомендация FastAPI docs. |
| Features | HIGH | Table stakes — industry standard. Telegram-специфичные ограничения подтверждены официальной документацией + Pentaract reference. |
| Architecture | HIGH | Официальные docs Telethon + FastAPI + Telegram MTProto spec. Pentaract как reference implementation подтверждает ключевые паттерны. |
| Pitfalls | HIGH для Telethon-специфичных (SQLite lock, file_id binding, FloodWait) — подтверждены GitHub issues. MEDIUM для rate limit точных значений (Telegram менял алгоритм в Bot API 7.0). |

**Overall confidence:** HIGH

### Gaps to Address

- **Точные лимиты FloodWait:** 20 req/30s — несколько источников согласны, но Telegram меняет алгоритм без уведомления. Проверить эмпирически при первом нагрузочном тесте.
- **FILE_REFERENCE_EXPIRED время жизни:** Официально не задокументировано. Реализовать retry-декоратор с первого дня, не ждать воспроизведения ошибки.
- **`iter_download` с chunked file_id:** Telethon docs подтверждают memory-efficient режим, но конкретное поведение при скачивании 20-МБ чанков (не полного файла) требует проверки в интеграционном тесте.
- **StringSession persistence:** При рестарте контейнера все Telethon-сессии теряются и требуют переподключения. Если store session_string в БД — определить schema и lifecycle в Phase 1.

---

## Sources

### Primary (HIGH confidence)
- [Telethon 1.42.0 docs](https://docs.telethon.dev/en/stable/) — asyncio patterns, session management, iter_download, FloodWaitError
- [Telegram MTProto File API](https://core.telegram.org/api/files) — chunk rules, parallel upload queues
- [Telegram Login Widget](https://core.telegram.org/widgets/login) — HMAC-SHA256 verification flow
- [FastAPI StreamingResponse docs](https://fastapi.tiangolo.com/advanced/custom-response/) — async generator pattern
- [FastAPI BackgroundTasks docs](https://fastapi.tiangolo.com/tutorial/background-tasks/)
- [SQLAlchemy PyPI 2.0.49](https://pypi.org/project/SQLAlchemy/) — версия подтверждена
- [asyncpg PyPI 0.31.0](https://pypi.org/project/asyncpg/) — версия подтверждена
- [PyJWT PyPI 2.12.1](https://pypi.org/project/PyJWT/) — версия подтверждена
- [FastAPI PyPI 0.135.3](https://pypi.org/project/fastapi/) — версия подтверждена
- [Telethon PyPI 1.42.0](https://pypi.org/project/Telethon/) — версия подтверждена

### Secondary (MEDIUM confidence)
- [Pentaract reference implementation](https://github.com/Dominux/Pentaract) — file_chunks schema, is_uploaded pattern, SQL rate limiting (Rust, адаптировано для Python)
- [TGstorage (DraxonV1)](https://github.com/DraxonV1/TGstorage) — FastAPI implementation patterns
- [FastAPI: python-jose → PyJWT migration](https://github.com/fastapi/fastapi/discussions/11345)
- [FastAPI + BackgroundTask + StreamingResponse conflict](https://github.com/fastapi/fastapi/discussions/11022)
- [Telethon GitHub #637](https://github.com/LonamiWebs/Telethon/issues/637) — SQLite database is locked
- [Telethon GitHub #3235](https://github.com/LonamiWebs/Telethon/issues/3235) — RAM growth
- [Telethon FAQ — FloodWait и параллельная загрузка](https://docs.telethon.dev/en/stable/quick-references/faq.html)

### Tertiary (LOW confidence)
- [asyncpg vs psycopg3 comparison](https://fernandoarteaga.dev/blog/psycopg-vs-asyncpg/) — blog, производительность asyncpg
- [Telethon upload speed gist](https://gist.github.com/painor/7e74de80ae0c819d3e9abcf9989a8dd6) — от контрибьютора Telethon, но неофициальный источник

---
*Research completed: 2026-04-06*
*Ready for roadmap: yes*
