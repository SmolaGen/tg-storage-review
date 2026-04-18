# Roadmap: tg-storage

## Overview

Сервис файлового хранилища на базе Telegram строится от фундамента к производственной готовности через 8 фаз. Зависимости жёсткие: схема БД с правильными колонками (`position`, `worker_id`, `message_id`) → аутентификация → Telegram-пул и хранилища → upload → download → управление файлами → bot-интерфейс → hardening. Каждая фаза доставляет полностью рабочую вертикальную возможность, проверяемую пользователем.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [x] **Phase 1: Foundation** - PostgreSQL схема, Docker Compose, конфигурация через pydantic-settings (completed 2026-04-06)
- [x] **Phase 2: Auth** - Telegram Login Widget + JWT access/refresh токены (completed 2026-04-06)
- [x] **Phase 3: Storage Management** - TelegramPool, регистрация хранилищ и workers с валидацией (completed 2026-04-06)
- [x] **Phase 4: Upload Pipeline** - Chunked параллельная загрузка файлов, 202 Accepted + polling (completed 2026-04-06)
- [x] **Phase 5: Download Pipeline** - StreamingResponse через iter_download, FILE_REFERENCE_EXPIRED retry (completed 2026-04-06)
- [x] **Phase 6: File Management** - Список файлов, soft delete, статусная фильтрация (completed 2026-04-06)
- [x] **Phase 7: Telegram Bot Interface** - Бот принимает файлы напрямую, отвечает ссылкой (completed 2026-04-06)
- [x] **Phase 8: Hardening** - Janitor job, health checks, мониторинг workers (completed 2026-04-06)
- [x] **Phase 9: Feature Expansion** - Папки, дубликаты, bulk-операции, расширенная фильтрация, улучшения скачивания, E2E-тесты (completed 2026-04-17)
- [ ] **Phase 10: UI Polish** - Loading states, обработка ошибок, поиск по имени, lightbox в галерее, форм a11y (4 плана: S01-S04)

## Phase Details

### Phase 1: Foundation
**Goal**: Проект запускается через docker-compose, база данных инициализирована с правильной схемой для всех последующих фаз
**Depends on**: Nothing (first phase)
**Requirements**: INFR-01, INFR-02
**Success Criteria** (what must be TRUE):
  1. `docker-compose up` поднимает FastAPI и PostgreSQL без ошибок
  2. Все 5 таблиц (`users`, `storages`, `storage_workers`, `files`, `file_chunks`) существуют с колонками `position`, `worker_id`, `message_id`, `tg_file_id`
  3. Alembic миграции применяются идемпотентно (`alembic upgrade head` дважды — без ошибок)
  4. StringSession колонка присутствует в `storage_workers` для хранения Telethon-сессий
**Plans**: 2 plans

Plans:
- [ ] 01-01: PostgreSQL схема и Alembic async миграции
- [ ] 01-02: FastAPI приложение с lifespan, pydantic-settings, Docker Compose

### Phase 2: Auth
**Goal**: Пользователь может войти через Telegram Login Widget и получить JWT токены для доступа к API
**Depends on**: Phase 1
**Requirements**: AUTH-01, AUTH-02, AUTH-03
**Success Criteria** (what must be TRUE):
  1. Пользователь отправляет данные Telegram Login Widget на `POST /auth/telegram` и получает `access_token` и `refresh_token`
  2. Запрос к защищённому эндпоинту с валидным `Bearer` токеном проходит, с невалидным — возвращает 401
  3. Пользователь обменивает `refresh_token` на новый `access_token` через `POST /auth/refresh`
  4. HMAC-SHA256 верификация Telegram данных отклоняет поддельный login payload
**Plans**: 1 plan

Plans:
- [ ] 02-01: AuthService (HMAC-SHA256 верификация, JWT issue/refresh), Auth Router, Bearer middleware

### Phase 3: Storage Management
**Goal**: Пользователь регистрирует Telegram-хранилище с ботами и сервис валидирует доступ к каналу
**Depends on**: Phase 2
**Requirements**: STOR-01, STOR-02, STOR-03, STOR-04
**Success Criteria** (what must be TRUE):
  1. Пользователь создаёт хранилище с `chat_id` и именем через `POST /storages` — хранилище появляется в `GET /storages`
  2. Пользователь добавляет `bot_token` к хранилищу через `POST /storages/{id}/workers` — сервис проверяет токен через `get_me()` и доступ бота к каналу
  3. Невалидный `bot_token` или бот без доступа к каналу возвращает 400 с понятным сообщением
  4. TelegramPool инициализирует StringSession и переподключается при разрыве без потери запросов
**Plans**: 2 plans

Plans:
- [ ] 03-01-PLAN.md: TelegramPool singleton + validate_and_register_worker + Pydantic-схемы
- [ ] 03-02-PLAN.md: StorageService CRUD + Storages Router + lifespan wiring + integration-тесты

### Phase 4: Upload Pipeline
**Goal**: Пользователь загружает файл любого размера до 2 ГБ через REST API — файл параллельно уходит в Telegram чанками
**Depends on**: Phase 3
**Requirements**: UPLD-01, UPLD-02, UPLD-03, UPLD-04, UPLD-05
**Success Criteria** (what must be TRUE):
  1. `POST /files/upload` возвращает `202 Accepted` с `file_id` немедленно, не дожидаясь загрузки
  2. Файл размером >20 МБ делится на чанки и каждый чанк загружается через отдельного worker-бота параллельно
  3. `GET /files/{id}/status` возвращает `is_uploaded=false` во время загрузки и `is_uploaded=true` после завершения
  4. `file_chunks` содержит запись для каждого чанка с `position`, `worker_id`, `tg_file_id`, `message_id`
  5. FloodWaitError обрабатывается автоматически с backoff, загрузка продолжается без ошибки 429 для клиента
**Plans**: TBD

Plans:
- [ ] 04-01: ChunkingService и FileService.upload_to_telegram (asyncio.gather + Semaphore)
- [ ] 04-02: Upload Router (POST /files/upload, GET /files/{id}/status, атомарный is_uploaded)

### Phase 5: Download Pipeline
**Goal**: Пользователь скачивает ранее загруженный файл — сервер стримит байты без буферизации
**Depends on**: Phase 4
**Requirements**: DWNL-01, DWNL-02, DWNL-03
**Success Criteria** (what must be TRUE):
  1. `GET /files/{id}/download` начинает стримить байты немедленно, Content-Length установлен корректно
  2. Файл, загруженный несколькими чанками через разных workers, скачивается целым и корректно собирается по `position`
  3. При `FILE_REFERENCE_EXPIRED` скачивание автоматически обновляет ссылку через `message_id` и продолжается прозрачно для клиента
  4. Каждый чанк скачивается тем же ботом (`worker_id`), которым был загружен
**Plans**: TBD

Plans:
- [ ] 05-01: Download Pipeline (async generator, StreamingResponse, iter_download, FILE_REFERENCE_EXPIRED retry)

### Phase 6: File Management
**Goal**: Пользователь видит список своих файлов и может удалять их
**Depends on**: Phase 4
**Requirements**: FILE-01, FILE-02, FILE-03
**Success Criteria** (what must be TRUE):
  1. `GET /files` возвращает файлы пользователя с метаданными (имя, размер, mime-type, дата, статус)
  2. Список содержит только файлы с `is_uploaded=true` — частично загруженные не видны
  3. `DELETE /files/{id}` помечает файл удалённым в БД, после чего файл исчезает из `GET /files`
**Plans**: TBD

Plans:
- [ ] 06-01: Files CRUD Router (GET /files с фильтрацией, DELETE /files/{id} soft delete)

### Phase 7: Telegram Bot Interface
**Goal**: Пользователь пересылает файл напрямую боту в Telegram и получает ссылку для скачивания
**Depends on**: Phase 4
**Requirements**: BOT-01, BOT-02
**Success Criteria** (what must be TRUE):
  1. Пользователь пересылает любой файл боту в Telegram — файл сохраняется в его хранилище так же, как при загрузке через REST API
  2. Бот отвечает ссылкой вида `GET /files/{id}/download` после успешного сохранения файла
**Plans**: TBD

Plans:
- [ ] 07-01: Bot webhook handler (приём forwarded-файлов, вызов FileService.ingest, ответ ссылкой)

### Phase 8: Hardening
**Goal**: Сервис надёжно работает в production: zombie-записи очищаются автоматически, workers мониторятся
**Depends on**: Phase 5, Phase 6, Phase 7
**Requirements**: INFR-03
**Success Criteria** (what must be TRUE):
  1. Записи с `is_uploaded=false` старше 1 часа автоматически помечаются как failed и не появляются в листинге
  2. `GET /health` возвращает статус каждого зарегистрированного worker-бота (доступен / недоступен)
**Plans**: TBD

Plans:
- [ ] 08-01: Janitor job (zombie cleanup, SQLAlchemy background task), health-check endpoint

### Phase 9: Feature Expansion
**Goal**: Сервис получает папки, обнаружение дубликатов, bulk-удаление, расширенную фильтрацию файлов, Unicode-имена при скачивании и E2E стресс-тесты — всё реализовано и задокументировано
**Depends on**: Phase 6, Phase 8
**Requirements**: EXP-01, EXP-02, EXP-03, EXP-04, EXP-05
**Success Criteria** (what must be TRUE):
  1. Пользователь создаёт, переименовывает, удаляет папки и перемещает файлы между ними через REST API
  2. `GET /files/duplicates?storage_id=...` возвращает группы файлов с одинаковым именем и размером
  3. `DELETE /files/bulk` удаляет несколько файлов атомарно за один запрос
  4. `GET /files` поддерживает фильтрацию по `mime_category`, `date_from`, `date_to`, `folder_id`, `root_only`
  5. Скачивание возвращает корректный `Content-Disposition` для Unicode-имён (RFC 5987); прогресс отображается в UI
  6. Playwright E2E-тесты покрывают auth, protected routes, concurrent requests, rate limiting, bulk delete и UI-сценарии
**Plans**: 2 plans

Plans:
- [ ] 09-01-PLAN.md — Верификация Folder Management + File Operations API (EXP-01, EXP-02, EXP-03, EXP-04)
- [ ] 09-02-PLAN.md — Верификация Download Improvements + E2E Tests (EXP-05)

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Foundation | 2/2 | Complete | 2026-04-06 |
| 2. Auth | 1/1 | Complete | 2026-04-06 |
| 3. Storage Management | 2/2 | Complete | 2026-04-06 |
| 4. Upload Pipeline | 2/2 | Complete | 2026-04-06 |
| 5. Download Pipeline | 1/1 | Complete | 2026-04-06 |
| 6. File Management | 1/1 | Complete | 2026-04-06 |
| 7. Telegram Bot Interface | 1/1 | Complete | 2026-04-06 |
| 8. Hardening | 1/1 | Complete | 2026-04-06 |
| 9. Feature Expansion | 2/2 | Complete   | 2026-04-17 |
