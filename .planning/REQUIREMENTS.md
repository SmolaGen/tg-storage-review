# Requirements: tg-storage

**Defined:** 2026-04-06
**Core Value:** Загружать и скачивать файлы через API, используя Telegram как бесплатное хранилище — без лимитов по объёму, без затрат на S3.

## v1 Requirements

### Authentication

- [ ] **AUTH-01**: Пользователь авторизуется через Telegram Login Widget → получает JWT access token (15 мин) и refresh token (30 дней)
- [ ] **AUTH-02**: Пользователь обновляет access token через refresh token
- [ ] **AUTH-03**: Все API эндпоинты требуют валидный Bearer токен

### Storage Management

- [ ] **STOR-01**: Пользователь создаёт хранилище, указав `chat_id` приватного Telegram-канала и имя
- [ ] **STOR-02**: Пользователь добавляет `bot_token` к хранилищу (worker) для загрузки файлов
- [ ] **STOR-03**: Сервис валидирует bot_token при регистрации (проверяет доступ к каналу)
- [ ] **STOR-04**: Пользователь видит список своих хранилищ и workers

### File Upload

- [ ] **UPLD-01**: Пользователь загружает файл через `POST /files/upload` (multipart) — сервис возвращает `202 Accepted` и `file_id` немедленно
- [ ] **UPLD-02**: Файл автоматически делится на чанки по 20 МБ и распределяется между доступными workers параллельно через `asyncio.gather`
- [ ] **UPLD-03**: Каждый чанк сохраняется в `file_chunks` с `position`, `worker_id`, `tg_file_id`, `message_id`
- [ ] **UPLD-04**: После загрузки всех чанков флаг `is_uploaded` устанавливается в `True` атомарно
- [ ] **UPLD-05**: Клиент может опросить статус загрузки через `GET /files/{id}/status`

### File Download

- [ ] **DWNL-01**: Пользователь скачивает файл через `GET /files/{id}/download` — сервис стримит байты через `StreamingResponse`
- [ ] **DWNL-02**: Чанки скачиваются параллельно через `iter_download()` теми же workers, что загружали, и собираются по `position`
- [ ] **DWNL-03**: При `FILE_REFERENCE_EXPIRED` сервис автоматически обновляет ссылку через `message_id` и повторяет скачивание

### File Management

- [ ] **FILE-01**: Пользователь получает список файлов своего хранилища с метаданными (имя, размер, mime-type, дата, статус)
- [ ] **FILE-02**: Пользователь удаляет файл — запись в БД помечается как удалённая (soft delete, Telegram сообщения остаются)
- [ ] **FILE-03**: Список файлов возвращает только файлы с `is_uploaded=True`

### Telegram Bot Interface

- [ ] **BOT-01**: Пользователь пересылает файл боту напрямую в Telegram — файл сохраняется в его хранилище как через REST API
- [ ] **BOT-02**: Бот отвечает ссылкой на скачивание файла через REST API

### Infrastructure

- [x] **INFR-01**: Сервис запускается через `docker-compose up` (FastAPI + PostgreSQL)
- [x] **INFR-02**: Telethon сессии хранятся как `StringSession` в PostgreSQL (не SQLite-файлы)
- [ ] **INFR-03**: Фоновый janitor-job очищает записи с `is_uploaded=False` старше 1 часа (zombie cleanup)

## v2 Requirements

### Performance

- **PERF-01**: Prefetch buffering для параллельного скачивания нескольких чанков заранее
- **PERF-02**: CDN-прокси для популярных файлов

### Advanced Features

- **ADV-01**: Resumable upload (tus-protocol) для загрузки файлов частями
- **ADV-02**: Дедупликация файлов по хешу (SHA-256)
- **ADV-03**: Версионирование файлов
- **ADV-04**: Иерархия папок
- **ADV-05**: Временные публичные ссылки с TTL

### Monitoring

- **MON-01**: Prometheus метрики (upload speed, download latency, worker utilization)
- **MON-02**: Health-check endpoint с состоянием workers

## Out of Scope

| Feature | Reason |
|---------|--------|
| S3-совместимый API | Сложность реализации без пропорциональной ценности для v1 |
| OAuth / email регистрация | Только Telegram Login — соответствует концепции |
| Публичный доступ к файлам | Архитектурно сложнее, не заявлено в требованиях |
| Удаление сообщений из Telegram | Bot API не позволяет боту удалять сообщения в каналах |
| Folder hierarchy v1 | Высокая сложность, деферировано |
| Real-time upload progress (WebSocket) | Polling достаточен для v1 |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| AUTH-01 | Phase 2 | Pending |
| AUTH-02 | Phase 2 | Pending |
| AUTH-03 | Phase 2 | Pending |
| STOR-01 | Phase 3 | Pending |
| STOR-02 | Phase 3 | Pending |
| STOR-03 | Phase 3 | Pending |
| STOR-04 | Phase 3 | Pending |
| UPLD-01 | Phase 4 | Pending |
| UPLD-02 | Phase 4 | Pending |
| UPLD-03 | Phase 4 | Pending |
| UPLD-04 | Phase 4 | Pending |
| UPLD-05 | Phase 4 | Pending |
| DWNL-01 | Phase 5 | Pending |
| DWNL-02 | Phase 5 | Pending |
| DWNL-03 | Phase 5 | Pending |
| FILE-01 | Phase 6 | Pending |
| FILE-02 | Phase 6 | Pending |
| FILE-03 | Phase 6 | Pending |
| BOT-01 | Phase 7 | Pending |
| BOT-02 | Phase 7 | Pending |
| INFR-01 | Phase 1 | Complete |
| INFR-02 | Phase 1 | Complete |
| INFR-03 | Phase 8 | Pending |

**Coverage:**
- v1 requirements: 23 total
- Mapped to phases: 23
- Unmapped: 0 ✓

---
*Requirements defined: 2026-04-06*
*Last updated: 2026-04-06 after initial research*
