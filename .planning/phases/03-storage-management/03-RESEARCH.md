# Phase 3: Storage Management - Research

**Researched:** 2026-04-06
**Domain:** Telethon MTProto bot authentication, FastAPI singleton pool, PostgreSQL CRUD
**Confidence:** HIGH

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| STOR-01 | Пользователь создаёт хранилище с `chat_id` приватного канала и именем | Storage model exists (id UUID, user_id, name, chat_id BigInt) — CRUD is straightforward |
| STOR-02 | Пользователь добавляет `bot_token` к хранилищу (worker) | StorageWorker model exists (bot_token, session_string, storage_id) — add worker + validate |
| STOR-03 | Сервис валидирует bot_token при регистрации (доступ к каналу) | TelegramClient(StringSession(), api_id, api_hash).start(bot_token=) + get_me() + get_entity(chat_id) |
| STOR-04 | Пользователь видит список хранилищ и workers | GET /storages returns Storage list with workers nested |
</phase_requirements>

---

## Summary

Phase 3 builds TelegramPool (singleton держатель Telethon-клиентов) и REST CRUD для хранилищ и воркеров. Ключевая сложность — правильная async инициализация `TelegramClient` с `bot_token` через MTProto (не Bot API), сохранение `session_string` в PostgreSQL после первого подключения, и перехват правильных Telethon-ошибок при валидации.

Существующие модели `Storage` и `StorageWorker` из Phase 1 полностью готовы: все нужные колонки присутствуют (`chat_id`, `bot_token`, `session_string`, `last_used_at`). Конфиг уже содержит `tg_api_id` и `tg_api_hash`. `get_current_user` из `app/api/deps.py` готов к использованию.

Паттерн тестирования — unit-тесты с `unittest.mock.AsyncMock` для TelegramClient (реальный Telegram недоступен в CI), integration-тесты на SQLite+aiosqlite для CRUD-слоя. Никаких новых test dependencies не требуется.

**Primary recommendation:** TelegramPool как синглтон в `app/state.py`, инициализируется в lifespan; валидация бота создаёт временный клиент, сохраняет session_string, затем регистрирует его в пуле.

---

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| telethon | 1.42.0 | MTProto клиент, bot_token auth, get_me(), get_entity() | Уже в requirements.txt; 2GB upload limit vs 50MB у Bot API |
| sqlalchemy | 2.0.49 | AsyncSession CRUD | Уже используется в Phase 1/2 |
| fastapi | 0.135.3 | Router, Depends | Уже используется |
| pydantic-settings | latest | Settings с tg_api_id/tg_api_hash | Уже в app/config.py |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| telethon.sessions.StringSession | встроен в telethon | Сериализация сессии в строку для хранения в БД | Везде — FileSession запрещён (нет persistent файлов в Docker) |
| telethon.errors | встроен в telethon | AccessTokenInvalidError, ChannelPrivateError, FloodWaitError | При валидации bot_token и chat_id |
| unittest.mock (AsyncMock) | stdlib | Мок TelegramClient в тестах | Все unit-тесты StorageService.add_worker() |
| aiosqlite | уже установлен (тесты Phase 2) | SQLite backend для тестовой БД | Все integration-тесты |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| StringSession в PostgreSQL | FileSession на диске | Файловые сессии несовместимы с Docker/Kubernetes — concurrent locks |
| asyncio.Semaphore(3) per bot | глобальный семафор | Per-bot семафор точнее отражает реальный лимит FloodWait |
| Singleton TelegramPool | per-request клиент | MTProto соединение дорогое, нельзя создавать на каждый запрос |

**Installation:** Все уже установлены. Новых зависимостей не требуется.

---

## Architecture Patterns

### Recommended Project Structure
```
app/
├── state.py             # TelegramPool singleton (dict[UUID, TelegramClient])
├── services/
│   └── storage.py       # StorageService: create_storage, add_worker, list_storages
├── api/
│   └── storages.py      # Router: POST /storages, POST /storages/{id}/workers, GET /storages
└── schemas/
    └── storage.py       # StorageCreate, WorkerCreate, StorageResponse, WorkerResponse
tests/
└── test_storage.py      # STOR-01..04 unit + integration tests
```

### Pattern 1: TelegramPool Singleton

**What:** Глобальный dict `_clients: dict[UUID, TelegramClient]` в `app/state.py`. Lazy init при первом обращении. Инициализируется из БД при старте FastAPI lifespan.

**When to use:** Везде, где нужен Telethon-клиент для конкретного worker_id.

```python
# app/state.py
from __future__ import annotations
import asyncio
from uuid import UUID
from telethon import TelegramClient
from telethon.sessions import StringSession

class TelegramPool:
    def __init__(self):
        self._clients: dict[UUID, TelegramClient] = {}
        self._semaphores: dict[UUID, asyncio.Semaphore] = {}

    async def get_or_create(
        self,
        worker_id: UUID,
        bot_token: str,
        session_string: str | None,
        api_id: int,
        api_hash: str,
    ) -> TelegramClient:
        if worker_id not in self._clients:
            session = StringSession(session_string) if session_string else StringSession()
            client = TelegramClient(session, api_id, api_hash)
            await client.start(bot_token=bot_token)
            self._clients[worker_id] = client
        return self._clients[worker_id]

    def semaphore(self, worker_id: UUID) -> asyncio.Semaphore:
        if worker_id not in self._semaphores:
            self._semaphores[worker_id] = asyncio.Semaphore(3)
        return self._semaphores[worker_id]

    async def remove(self, worker_id: UUID) -> None:
        client = self._clients.pop(worker_id, None)
        self._semaphores.pop(worker_id, None)
        if client:
            await client.disconnect()

    async def shutdown(self) -> None:
        for client in self._clients.values():
            await client.disconnect()
        self._clients.clear()
        self._semaphores.clear()

# Global singleton
telegram_pool = TelegramPool()
```

### Pattern 2: Bot Validation Flow (STOR-02, STOR-03)

**What:** При регистрации worker сервис создаёт временный TelegramClient, проверяет бота и доступ к каналу, сохраняет session_string, затем добавляет клиент в пул.

**When to use:** POST /storages/{id}/workers

```python
# app/services/storage.py — validate_and_register_worker()
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    AccessTokenInvalidError,
    AccessTokenExpiredError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    UserDeactivatedBanError,
)

async def validate_and_register_worker(
    bot_token: str,
    chat_id: int,
    api_id: int,
    api_hash: str,
) -> str:
    """
    Returns session_string after successful validation.
    Raises ValueError with user-friendly message on failure.
    """
    session = StringSession()
    client = TelegramClient(session, api_id, api_hash)
    try:
        await client.start(bot_token=bot_token)       # шаг 1: авторизация
        me = await client.get_me()                     # шаг 2: подтверждение бота
        if me is None:
            raise ValueError("Bot not found")
        await client.get_entity(chat_id)               # шаг 3: доступ к каналу
        return session.save()                          # шаг 4: сохранить session_string
    except (AccessTokenInvalidError, AccessTokenExpiredError):
        raise ValueError("Invalid or expired bot_token")
    except ChannelPrivateError:
        raise ValueError("Bot does not have access to the channel. Add bot as admin.")
    except ChatAdminRequiredError:
        raise ValueError("Bot must be an admin in the channel.")
    except FloodWaitError as e:
        raise ValueError(f"Telegram rate limit: retry after {e.seconds}s")
    except UserDeactivatedBanError:
        raise ValueError("Bot account is banned")
    except Exception as e:
        raise ValueError(f"Telegram validation failed: {e}")
    finally:
        await client.disconnect()
```

### Pattern 3: StringSession Save/Load

**What:** `session.save()` возвращает строку, которую сохраняем в `storage_workers.session_string`. При загрузке передаём строку в `StringSession(string)`.

```python
# Сохранение (после первого start)
session_str = client.session.save()  # возвращает строку типа "1BJW..."

# Загрузка
from telethon.sessions import StringSession
client = TelegramClient(StringSession(session_str), api_id, api_hash)
await client.connect()  # НЕ нужен start() — уже авторизован
```

**Важно:** `StringSession` с уже сохранённой строкой при `connect()` не требует повторного `start(bot_token=)`. Это позволяет переподключаться без новой авторизации.

### Pattern 4: lifespan — инициализация пула из БД

```python
# app/main.py
from contextlib import asynccontextmanager
from app.state import telegram_pool
from app.database import async_session
from app.models.storage_worker import StorageWorker
from sqlalchemy import select

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Загружаем всех воркеров из БД и инициализируем клиентов
    async with async_session() as session:
        result = await session.execute(
            select(StorageWorker).where(StorageWorker.session_string.is_not(None))
        )
        workers = result.scalars().all()
        for w in workers:
            await telegram_pool.get_or_create(
                w.id, w.bot_token, w.session_string, settings.tg_api_id, settings.tg_api_hash
            )
    yield
    await telegram_pool.shutdown()
```

### Anti-Patterns to Avoid

- **Создавать TelegramClient на каждый запрос:** MTProto handshake дорог (~1-2 сек), клиент должен жить в пуле
- **Использовать FileSession в Docker:** файлы теряются при перезапуске контейнера; только StringSession в PostgreSQL
- **Не отключать клиент после валидации:** утечка соединений — обязательно `finally: await client.disconnect()`
- **Полагаться на auto-sleep Telethon для FloodWait:** в production FloodWait > 60s надо перехватывать явно
- **Хранить bot_token в открытом виде без unique constraint:** UniqueConstraint("storage_id", "bot_token") уже есть в модели
- **Не проверять принадлежность хранилища пользователю:** перед добавлением worker обязательно проверить `storage.user_id == current_user.id`, иначе IDOR

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| MTProto авторизация | Собственный auth | `client.start(bot_token=)` | TL-схема, шифрование, DH-обмен — тысячи строк |
| Сериализация сессии | Кастомный формат | `StringSession.save()` / `StringSession(str)` | Telethon сам правильно сериализует все ключи и salt |
| Декодирование chat_id | Ручная математика | `client.get_entity(chat_id)` | Telegram peer IDs для каналов требуют prefix -100 |
| Retry при FloodWait | Кастомный retry loop | `flood_sleep_threshold` параметр TelegramClient | Telethon сам спит при FloodWait < threshold |
| Валидация UUID | Ручная проверка | Pydantic UUID тип в схемах | Pydantic v2 валидирует и сериализует UUID |

**Key insight:** Telethon инкапсулирует всю сложность MTProto. Задача — правильно lifecycle-управлять клиентами, не пересоздавая логику протокола.

---

## Common Pitfalls

### Pitfall 1: Неправильный chat_id формат для каналов

**What goes wrong:** `get_entity(-1001234567890)` работает, но `get_entity(1234567890)` (без prefix -100) может не найти канал или найти не то.

**Why it happens:** Telegram внутренне хранит channel IDs без prefix, но Bot API возвращает их с `-100`. Telethon принимает оба формата, но ведёт себя по-разному.

**How to avoid:** Принимать `chat_id` как BigInteger от пользователя в том виде, в котором он пришёл из Telegram (обычно с -100). Не преобразовывать. `get_entity()` в Telethon понимает оба формата.

**Warning signs:** `ValueError: Cannot find any entity corresponding to...`

### Pitfall 2: `start()` vs `connect()` для уже авторизованных сессий

**What goes wrong:** Вызов `client.start(bot_token=)` для клиента с уже сохранённым StringSession приводит к повторной авторизации и обновлению сессии.

**Why it happens:** `start()` всегда пытается войти. Для уже авторизованных клиентов нужен только `connect()`.

**How to avoid:**
- Первый запуск (новый воркер): `await client.start(bot_token=bot_token)` → `session.save()`
- Переподключение (существующий session_string): `await client.connect()` — без `start()`

**Warning signs:** При повторном `start(bot_token=)` иногда появляется `AuthKeyDuplicatedError`.

### Pitfall 3: get_entity() требует участия в канале

**What goes wrong:** `get_entity(chat_id)` для канала, где бот не является участником/админом, бросает `ChannelPrivateError` или `ValueError`.

**Why it happens:** MTProto кэширует entities только для известных каналов. Бот должен быть добавлен в канал как admin с правом постинга.

**How to avoid:** Документировать пользователю: бот должен быть добавлен в канал как admin **до** вызова POST /storages/{id}/workers. Перехватывать `ChannelPrivateError` → 400.

**Warning signs:** `ChannelPrivateError` в логах при валидации.

### Pitfall 4: IDOR — проверка owner хранилища

**What goes wrong:** POST /storages/{storage_id}/workers без проверки `storage.user_id == current_user.id` позволяет добавлять workers в чужое хранилище.

**Why it happens:** storage_id — UUID, легко угадать или перебрать.

**How to avoid:** В `StorageService.add_worker()` всегда делать `SELECT storage WHERE id=storage_id AND user_id=current_user.id`. Если не найдено → 404 (не 403, чтобы не раскрывать существование).

**Warning signs:** Endpoint возвращает 200 для чужого storage_id.

### Pitfall 5: SQLite несовместим с pg_insert для тестов

**What goes wrong:** `from sqlalchemy.dialects.postgresql import insert as pg_insert` падает в SQLite-тестах.

**Why it happens:** Phase 2 решила эту проблему через стандартный `session.add()` там где не нужен upsert. StorageService тоже должен использовать обычный `session.add()` для новых записей.

**How to avoid:** Storage и StorageWorker создаются через `session.add(obj)` — upsert не нужен (нет конфликта по PK). Это совместимо с SQLite.

---

## Code Examples

Verified patterns from official Telethon docs and project context:

### Создание TelegramClient с StringSession (новая сессия)
```python
# Source: Telethon 1.42.0 docs — Signing In
from telethon import TelegramClient
from telethon.sessions import StringSession

session = StringSession()  # пустая сессия
client = TelegramClient(session, api_id=settings.tg_api_id, api_hash=settings.tg_api_hash)
await client.start(bot_token="1234567890:AABBccDD...")
session_string = session.save()  # → "1BJWap1..."
```

### Восстановление клиента из сохранённой сессии
```python
# Source: Telethon 1.42.0 docs — Session Files
from telethon.sessions import StringSession

session = StringSession(saved_string)  # строка из БД
client = TelegramClient(session, api_id, api_hash)
await client.connect()  # НЕ start() — уже авторизован
```

### Перехват ошибок при валидации
```python
# Source: Telethon errors.csv (v1 branch GitHub)
from telethon.errors import (
    AccessTokenInvalidError,   # 400: "The provided token is not valid"
    AccessTokenExpiredError,   # 400: "Bot token expired"
    ChannelPrivateError,       # 400/406: нет доступа к каналу
    ChatAdminRequiredError,    # 400/403: нужны права админа
    FloodWaitError,            # 420: e.seconds — сколько ждать
    UserDeactivatedBanError,   # 401: бот заблокирован
)
```

### StorageWorker создание через session.add (SQLite-совместимо)
```python
# Совместимо с тестами на SQLite
from app.models.storage_worker import StorageWorker

worker = StorageWorker(
    storage_id=storage_id,
    bot_token=bot_token,
    session_string=session_string,  # из validate_and_register_worker()
)
session.add(worker)
await session.commit()
await session.refresh(worker)
return worker
```

### GET /storages endpoint с get_current_user
```python
# Pattern established in Phase 2: deps.py
from app.api.deps import get_current_user

@router.get("/storages", response_model=list[StorageResponse])
async def list_storages(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    ...
```

### Pydantic schema с UUID и chat_id BigInteger
```python
from pydantic import BaseModel
from uuid import UUID

class StorageCreate(BaseModel):
    name: str
    chat_id: int  # BigInteger — принимаем как есть от пользователя

class WorkerCreate(BaseModel):
    bot_token: str

class StorageResponse(BaseModel):
    id: UUID
    name: str
    chat_id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| FileSession ('bot.session' файл) | StringSession в PostgreSQL | Telethon 1.x | Docker-совместимо, нет file locking |
| @app.on_event("startup") | lifespan asynccontextmanager | FastAPI 0.95+ | Уже используется в Phase 1/2 |
| sync TelegramClient | async TelegramClient + asyncio | Telethon 1.x async | Единственный правильный путь для FastAPI |
| HTTPException(403) для not-owner | HTTPException(404) | Стандарт security | Не раскрывает факт существования ресурса |

**Deprecated/outdated:**
- `from telethon.sync import TelegramClient`: sync wrapper — никогда в asyncio контексте FastAPI
- `client.run_until_disconnected()`: blocking call, несовместим с FastAPI event loop

---

## Open Questions

1. **Нужна ли полная загрузка всех workers при lifespan?**
   - Что знаем: в Phase 4 все workers должны быть готовы к upload
   - Что неясно: при большом количестве workers lifespan может быть медленным
   - Рекомендация: загружать при lifespan для Phase 3; в Phase 8 можно добавить lazy init

2. **Что возвращает get_entity() для channel — и достаточно ли этого для проверки доступа?**
   - Что знаем: ChannelPrivateError бросается при недоступном канале
   - Что неясно: бот может получить entity, но не иметь прав на постинг
   - Рекомендация: get_entity() достаточно для Phase 3; права на постинг проверятся в Phase 4 при реальной загрузке

3. **Нужен ли уникальный индекс на Storage.chat_id per user?**
   - Что знаем: модель имеет UniqueConstraint только на (storage_id, bot_token) в workers
   - Что неясно: может ли пользователь создать два хранилища с одним chat_id?
   - Рекомендация: для Phase 3 допустить — добавить constraint в Phase 8 если нужно

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio 0.x (уже установлен) |
| Config file | `pyproject.toml` — `[tool.pytest.ini_options]` asyncio_mode="auto" |
| Quick run command | `pytest tests/test_storage.py -x -q` |
| Full suite command | `pytest tests/ -q` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| STOR-01 | POST /storages создаёт хранилище в БД | integration | `pytest tests/test_storage.py::test_create_storage -x` | ❌ Wave 0 |
| STOR-01 | POST /storages без токена → 401 | integration | `pytest tests/test_storage.py::test_create_storage_unauthorized -x` | ❌ Wave 0 |
| STOR-01 | GET /storages возвращает только хранилища текущего пользователя | integration | `pytest tests/test_storage.py::test_list_storages_isolation -x` | ❌ Wave 0 |
| STOR-02 | POST /storages/{id}/workers с валидным bot_token → 201 | unit (mocked Telegram) | `pytest tests/test_storage.py::test_add_worker_valid -x` | ❌ Wave 0 |
| STOR-02 | StorageWorker.session_string сохранён в БД | integration | `pytest tests/test_storage.py::test_worker_session_saved -x` | ❌ Wave 0 |
| STOR-02 | POST /storages/{id}/workers для чужого хранилища → 404 | integration | `pytest tests/test_storage.py::test_add_worker_wrong_owner -x` | ❌ Wave 0 |
| STOR-03 | Невалидный bot_token → 400 | unit (mocked Telegram) | `pytest tests/test_storage.py::test_add_worker_invalid_token -x` | ❌ Wave 0 |
| STOR-03 | Бот без доступа к каналу → 400 | unit (mocked Telegram) | `pytest tests/test_storage.py::test_add_worker_no_channel_access -x` | ❌ Wave 0 |
| STOR-04 | GET /storages возвращает список с workers | integration | `pytest tests/test_storage.py::test_list_storages_with_workers -x` | ❌ Wave 0 |
| STOR-04 | GET /storages/{id}/workers возвращает workers хранилища | integration | `pytest tests/test_storage.py::test_list_workers -x` | ❌ Wave 0 |

### Мок-паттерн для TelegramClient (unit-тесты STOR-02/03)

```python
# tests/test_storage.py — Telethon мокается через AsyncMock
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telethon.errors import AccessTokenInvalidError, ChannelPrivateError

@pytest.mark.asyncio
async def test_validate_worker_invalid_token():
    from app.services.storage import validate_and_register_worker

    with patch("app.services.storage.TelegramClient") as MockClient:
        instance = AsyncMock()
        instance.start.side_effect = AccessTokenInvalidError(request=None)
        MockClient.return_value = instance
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(ValueError, match="Invalid or expired bot_token"):
            await validate_and_register_worker(
                bot_token="bad_token", chat_id=-1001234567890,
                api_id=12345, api_hash="abc"
            )

@pytest.mark.asyncio
async def test_validate_worker_no_channel_access():
    from app.services.storage import validate_and_register_worker

    with patch("app.services.storage.TelegramClient") as MockClient:
        instance = AsyncMock()
        instance.start = AsyncMock()
        instance.get_me = AsyncMock(return_value=MagicMock(is_bot=True))
        instance.get_entity.side_effect = ChannelPrivateError(request=None)
        instance.disconnect = AsyncMock()
        MockClient.return_value = instance

        with pytest.raises(ValueError, match="access to the channel"):
            await validate_and_register_worker(
                bot_token="valid_token", chat_id=-1001111111111,
                api_id=12345, api_hash="abc"
            )
```

### Sampling Rate
- **Per task commit:** `pytest tests/test_storage.py -x -q`
- **Per wave merge:** `pytest tests/ -q`
- **Phase gate:** Full suite green (`pytest tests/ -q`) перед `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_storage.py` — покрывает STOR-01, STOR-02, STOR-03, STOR-04
- [ ] `app/state.py` — TelegramPool singleton (создаётся в Wave 0 или Task 1 плана 03-01)
- [ ] Нет новых зависимостей — `aiosqlite` уже установлен из Phase 2, `unittest.mock` stdlib

---

## Sources

### Primary (HIGH confidence)
- Telethon 1.42.0 PyPI + docs — версии, StringSession, bot_token auth pattern
- `github.com/LonamiWebs/Telethon/blob/v1/telethon_generator/data/errors.csv` — точные имена классов ошибок: AccessTokenInvalidError, AccessTokenExpiredError, ChannelPrivateError, ChatAdminRequiredError, FloodWaitError, UserDeactivatedBanError
- `app/models/storage.py`, `app/models/storage_worker.py` — прочитаны напрямую, колонки подтверждены
- `app/api/deps.py` — get_current_user pattern прочитан напрямую
- `app/config.py` — tg_api_id, tg_api_hash, tg_bot_token подтверждены
- `tests/conftest.py` — SQLite+aiosqlite тестовый паттерн подтверждён из Phase 2

### Secondary (MEDIUM confidence)
- WebSearch: Telethon async connect/disconnect vs start() — подтверждено несколькими источниками
- WebSearch: FastAPI lifespan singleton pattern — подтверждено официальной документацией FastAPI

### Tertiary (LOW confidence)
- Поведение `get_entity()` для неадминского бота в канале — описание из WebSearch/GitHub issues, не из официальных docs. Потребует проверки при первом интеграционном тесте с реальным ботом.

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — все библиотеки уже в requirements.txt, версии из pypi/requirements.txt
- Architecture: HIGH — StringSession/start() паттерн из официальной Telethon docs; CRUD паттерн из Phase 2
- Error handling: HIGH — имена классов из errors.csv в официальном GitHub репозитории
- Pitfalls: MEDIUM — IDOR и chat_id format из общих best practices, get_entity() поведение LOW (нет официальной гарантии)

**Research date:** 2026-04-06
**Valid until:** 2026-05-06 (Telethon 1.42 стабильный, FastAPI паттерны устоявшиеся)
