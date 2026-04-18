# tg-storage

## What This Is

Сервис файлового хранилища, использующий Telegram как бесплатный безлимитный бэкенд. Пользователи подключают свои Telegram-боты и приватные каналы, а сервис предоставляет REST API и Telegram-бот интерфейс для загрузки и скачивания файлов любого размера. Мульти-тенантная архитектура: каждый пользователь изолирован в своём Telegram-пространстве.

## Core Value

Загружать и скачивать файлы через API, используя Telegram как бесплатное хранилище — без лимитов по объёму, без затрат на S3.

## Requirements

### Validated

- ✓ Пользователь регистрируется и авторизуется через Telegram Login → получает JWT — Phase 2

### Active

- [ ] Пользователь регистрируется и авторизуется через Telegram Login → получает JWT
- [ ] Пользователь подключает свой bot_token и chat_id приватного канала (хранилище)
- [ ] Пользователь добавляет несколько bot_token для балансировки нагрузки
- [ ] Пользователь загружает файл через REST API — файл режется на чанки 20 МБ и параллельно уходит на разных ботов в Telegram
- [ ] Пользователь загружает файл через Telegram бота напрямую (пересылает файл боту)
- [ ] Пользователь скачивает файл через REST API — сервер стримит байты напрямую
- [ ] Пользователь видит список своих файлов с метаданными
- [ ] Пользователь удаляет файл

### Out of Scope

- S3-совместимый API — достаточно собственного REST API
- OAuth/Email регистрация — только Telegram Login
- Публичный доступ к файлам — только приватный (по токену)
- Ручной чанкинг >2 ГБ — Telethon обрабатывает через MTProto нативно

## Context

**Референс:** Pentaract (Rust, 1.7k⭐) — ближайший аналог. Ключевые паттерны взяты оттуда:
- `file_chunks` таблица с `position` + `worker_id` для сборки при скачивании
- Флаг `is_uploaded=False` до завершения загрузки всех чанков
- Rolling window rate limiting через SQL (без Redis)

**Протокол:** Telethon с bot_token через MTProto (не HTTP Bot API) — даёт 2 ГБ лимит вместо 50 МБ.

**Загрузка файлов:** asyncio.gather для параллельной отправки чанков разными ботами. Каждый чанк скачивается тем же ботом, что загружал (Telegram привязывает file_id к боту).

## Constraints

- **Stack**: FastAPI + Python, PostgreSQL, Telethon — строго по профилю разработчика
- **Deploy**: Docker Compose, аналогично другим проектам
- **Auth**: Telegram Login Widget + JWT (access 15 мин, refresh 30 дней)
- **File size**: до 2 ГБ на файл (лимит MTProto)
- **Чанк**: 20 МБ (лимит Telegram для загрузки через Bot API файлов)

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Telethon MTProto вместо Bot API | 2 ГБ лимит vs 50 МБ | — Pending |
| Каждый пользователь = свои боты | Изоляция, нет shared rate limit | — Pending |
| file_id привязан к боту-загрузчику | Telegram требует тот же бот для скачивания | — Pending |
| StreamingResponse для скачивания | Минимальный latency, нет буферизации на сервере | — Pending |
| Чанки 20 МБ | Совместимость с Telegram ограничениями + параллельность | — Pending |
| HMAC secret = sha256(bot_token).digest() | Telegram spec требует raw bytes, не hex | Phase 2 |
| JWT encode(algorithm=) / decode(algorithms=[]) | PyJWT API асимметрия — критическая | Phase 2 |
| get_current_user в app/api/deps.py | Единая точка аутентификации для всех фаз | Phase 2 |

---
*Last updated: 2026-04-06 after Phase 2*
