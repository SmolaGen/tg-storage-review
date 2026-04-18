# Stack Research

**Domain:** Telegram-backed file storage service (FastAPI + MTProto)
**Researched:** 2026-04-06
**Confidence:** HIGH (core stack verified via PyPI + official docs)

---

## Recommended Stack

### Core Technologies

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| FastAPI | 0.135.3 | HTTP API framework | Async-first, native StreamingResponse для file streaming, Pydantic v2 out of box |
| Telethon | 1.42.0 | Telegram MTProto client | Единственный зрелый Python MTProto-клиент с поддержкой bot_token; даёт 2 ГБ лимит vs 50 МБ у HTTP Bot API |
| SQLAlchemy | 2.0.49 | ORM + async engine | Async engine поверх asyncpg, Alembic autogenerate, декларативные модели — лучший trade-off между скоростью и maintainability |
| asyncpg | 0.31.0 | PostgreSQL async driver | 5x быстрее psycopg2 по бенчмаркам; SQLAlchemy 2.0 использует его как backend через `asyncpg://` |
| Alembic | 1.18.4 | DB migrations | Официальный инструмент для SQLAlchemy; поддерживает async template (`alembic init -t async`) |
| PostgreSQL | 17 | Primary database | file_chunks + метаданные, rate limiting через SQL (rolling window без Redis), JSONB для bot_tokens |

### Supporting Libraries

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| PyJWT | 2.12.1 | JWT access/refresh tokens | Генерация и верификация JWT; python-jose больше не рекомендуется (заброшен, уязвимости) |
| cryptg | 0.5.2 | Ускорение Telegram AES-IGE | Обязательно — без него upload/download ~0.38 MB/s; с ним 3-10x быстрее. Telethon подхватывает автоматически |
| pydantic | 2.x (встроен в FastAPI) | Валидация входящих данных | Схемы для file metadata, bot credentials, Telegram Login payload |
| python-multipart | 0.0.x | Multipart file upload | Нужен FastAPI для приёма `UploadFile` через form-data |
| bcrypt | 4.x | Хэширование (если пароли нужны) | passlib брошен — использовать bcrypt напрямую; для этого проекта не нужен (только Telegram auth) |
| pytest-asyncio | 1.3.0 | Async тесты | Поддержка `async def` тестов и async fixtures; требует `asyncio_mode = "auto"` в pytest.ini |
| httpx | 0.28.1 | HTTP-клиент в тестах | AsyncClient для интеграционных тестов FastAPI (замена TestClient для async endpoints) |
| pytest | 8.x | Тест-раннер | Стандарт; конфигурация через `pyproject.toml` |

### Development Tools

| Tool | Purpose | Notes |
|------|---------|-------|
| Docker Compose | Оркестрация сервисов | API + PostgreSQL + pgAdmin; аналогично другим проектам в стеке |
| Ruff | Линтер + форматтер | Заменяет flake8 + black + isort одним инструментом, в 10-100x быстрее |
| mypy | Статическая типизация | Особенно важно для async кода — выявляет проблемы с coroutine/awaitable |
| alembic | Миграции БД | `alembic init -t async` — async template для работы с AsyncEngine |

---

## Installation

```bash
# Core
pip install fastapi==0.135.3 uvicorn[standard]
pip install telethon==1.42.0 cryptg==0.5.2
pip install sqlalchemy==2.0.49 asyncpg==0.31.0 alembic==1.18.4
pip install PyJWT==2.12.1
pip install python-multipart

# Dev
pip install pytest pytest-asyncio==1.3.0 httpx==0.28.1
pip install ruff mypy
```

---

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| Telethon 1.42.0 | Pyrogram | Если нужна более Pythonic/OOP-ориентированная API — Pyrogram получил второй импульс в 2024. Для файлового хранилища разницы нет, Telethon более проверен |
| Telethon 1.42.0 | Telethon 2.0.0a0 | Никогда в продакшене — альфа-релиз, API нестабилен, документация неполная |
| SQLAlchemy async | Raw asyncpg | При >15k rps и экстремально простых запросах. Для этого проекта ORM экономит недели разработки без заметных потерь производительности |
| asyncpg (через SQLAlchemy) | psycopg3 напрямую | psycopg3 удобнее для Pydantic row factories, но asyncpg быстрее и лучше поддерживается SQLAlchemy 2.0 |
| PyJWT | python-jose | python-jose выпустил 3.5.0 в мае 2025 после 4 лет молчания — ненадёжен; FastAPI docs официально переключились на PyJWT |
| Ruff | flake8 + black + isort | Только если уже настроен legacy-проект с ними |

---

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| python-jose | Заброшен на 4 года, уязвимости CVE, FastAPI docs убрали его; нестабильная поддержка | PyJWT 2.12.1 |
| passlib | Не поддерживается, deprecated error на Python 3.11+, будет удалён в 3.13 | bcrypt напрямую или pwdlib |
| Telethon 2.0.0a0 | Альфа-релиз (октябрь 2025), нестабильный API, missing features | Telethon 1.42.0 (stable, ноябрь 2025) |
| HTTP Bot API (python-telegram-bot/aiogram) | Лимит 50 МБ на файл — не подходит для файлового хранилища | Telethon MTProto |
| Redis | PROJECT.md явно указывает rolling window rate limiting через SQL без Redis — экономит сервис | SQL rolling window (паттерн Pentaract) |
| Celery / Dramatiq | Параллельная загрузка чанков — через asyncio.gather, не task queue | asyncio.gather с несколькими TelegramClient |

---

## Stack Patterns by Variant

**Параллельная загрузка чанков:**
- Инициализировать отдельный `TelegramClient` на каждый bot_token
- `asyncio.gather(*[upload_chunk(bot, chunk) for bot, chunk in zip(cycle(bots), chunks)])`
- file_id привязан к боту — хранить `worker_id` в `file_chunks` таблице
- Скачивать чанки тем же ботом, что загружал

**Streaming download:**
- `StreamingResponse(async_generator, media_type="application/octet-stream")`
- Async generator запрашивает чанки у Telethon последовательно по `position`
- Никакой буферизации на сервере — прямой pipe Telegram → HTTP client

**Telegram Login Widget verification:**
- Без сторонних библиотек: стандартная библиотека `hmac` + `hashlib`
- `secret_key = SHA256(bot_token)`, `check = HMAC-SHA256(data_check_string, secret_key)`
- `hmac.compare_digest()` для защиты от timing attacks
- Проверять `auth_date` — данные валидны 24 часа

**Async migrations:**
```bash
alembic init -t async alembic
# env.py: использовать async_engine_from_config + run_sync
```

---

## Version Compatibility

| Package | Compatible With | Notes |
|---------|-----------------|-------|
| SQLAlchemy 2.0.49 | asyncpg 0.31.0 | Подключение: `postgresql+asyncpg://user:pass@host/db` |
| FastAPI 0.135.3 | Pydantic 2.x | FastAPI 0.100+ требует Pydantic v2 |
| Telethon 1.42.0 | Python 3.10+ | Requires asyncio; Python 3.8 поддерживается, но 3.10+ рекомендуется |
| pytest-asyncio 1.3.0 | Python 3.10+ | Нужен `asyncio_mode = "auto"` в `pyproject.toml` |
| Alembic 1.18.4 | Python 3.10+ | |
| cryptg 0.5.2 | Telethon любой | Опциональная зависимость — Telethon автодетектирует |

---

## Telegram-Specific Notes

### MTProto vs HTTP Bot API (HIGH confidence)
MTProto через Telethon даёт:
- **2 ГБ** на файл (vs 50 МБ в HTTP Bot API)
- Прямое соединение с серверами Telegram (не через шлюз bot.telegram.org)
- Полный доступ к raw API (нет ограничений публичного Bot API)

### bot_token в Telethon (HIGH confidence)
```python
client = TelegramClient("session_name", api_id, api_hash)
await client.start(bot_token=token)
```
`api_id` и `api_hash` получить на https://my.telegram.org — обязательно даже для бота.

### Тест-стратегия для Telegram-интеграции (MEDIUM confidence)
- Unit-тесты: мокировать `TelegramClient` через `unittest.mock.AsyncMock`
- Integration-тесты: тестовый приватный канал + реальный bot_token (отдельный `.env.test`)
- Никогда не мокировать file_id поведение — оно зависит от реального бота-загрузчика

---

## Sources

- [Telethon PyPI (v1.42.0, Nov 2025)](https://pypi.org/project/Telethon/) — версия подтверждена
- [asyncpg PyPI (v0.31.0, Nov 2025)](https://pypi.org/project/asyncpg/) — версия подтверждена
- [PyJWT PyPI (v2.12.1, Mar 2026)](https://pypi.org/project/PyJWT/) — версия подтверждена
- [SQLAlchemy PyPI (v2.0.49, Apr 2026)](https://pypi.org/project/SQLAlchemy/) — версия подтверждена
- [Alembic PyPI (v1.18.4, Feb 2026)](https://pypi.org/project/alembic/) — версия подтверждена
- [FastAPI PyPI (v0.135.3, Apr 2026)](https://pypi.org/project/fastapi/) — версия подтверждена
- [cryptg PyPI (v0.5.2, Oct 2025)](https://pypi.org/project/cryptg/) — версия подтверждена
- [pytest-asyncio PyPI (v1.3.0, Nov 2025)](https://pypi.org/project/pytest-asyncio/) — версия подтверждена
- [FastAPI discussion: python-jose → PyJWT](https://github.com/fastapi/fastapi/discussions/11345) — MEDIUM confidence (community discussion)
- [passlib abandonment discussion](https://github.com/fastapi/fastapi/discussions/11773) — MEDIUM confidence
- [asyncpg vs psycopg3 comparison](https://fernandoarteaga.dev/blog/psycopg-vs-asyncpg/) — LOW confidence (blog)
- [Alembic async setup guide](https://berkkaraal.com/blog/2024/09/19/setup-fastapi-project-with-async-sqlalchemy-2-alembic-postgresql-and-docker/) — MEDIUM confidence
- [Telethon upload speed optimization gist](https://gist.github.com/painor/7e74de80ae0c819d3e9abcf9989a8dd6) — MEDIUM confidence (from Telethon contributor)
- [Telegram Login Widget official docs](https://core.telegram.org/widgets/login) — HIGH confidence

---
*Stack research for: tg-storage (Telegram-backed file storage)*
*Researched: 2026-04-06*
