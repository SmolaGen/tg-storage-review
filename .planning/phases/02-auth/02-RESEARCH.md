# Phase 2: Auth - Research

**Researched:** 2026-04-06
**Domain:** FastAPI JWT authentication via Telegram Login Widget
**Confidence:** HIGH

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**JWT Configuration:**
- Алгоритм подписи: HS256 (симметричный ключ из env var `JWT_SECRET_KEY`)
- Access token lifetime: 15 минут
- Refresh token lifetime: 30 дней
- Refresh tokens хранятся в БД (таблица `refresh_tokens` — UUID id, user_id FK, token TEXT, expires_at, created_at) — возможность инвалидации при logout

**Telegram Auth Security:**
- Таймаут валидности Telegram Login данных: 86400 секунд (24ч) — проверять `auth_date` из payload
- Открытая регистрация — любой Telegram пользователь может войти
- Автосоздание user при первом логине: upsert по `telegram_id` (INSERT ON CONFLICT DO UPDATE)
- HMAC-SHA256 верификация через stdlib `hmac` + `hashlib` (без сторонних библиотек)

**API Design:**
- `POST /auth/telegram` response: `{access_token, refresh_token, token_type: "bearer", expires_in: 900, user: {id, telegram_id, username}}`
- Bearer token: стандартный заголовок `Authorization: Bearer <token>` (FastAPI OAuth2PasswordBearer совместим)
- 401 с `{"detail": "Invalid or expired token"}` при невалидном/просроченном токене
- FastAPI Dependency `get_current_user` — переиспользуется во всех последующих фазах

### Claude's Discretion
- Структура файлов: `app/services/auth.py`, `app/api/auth.py`, `app/models/refresh_token.py`
- Alembic миграция для таблицы `refresh_tokens` (новая миграция поверх Phase 1)
- Тесты для HMAC верификации и JWT issue/verify

### Deferred Ideas (OUT OF SCOPE)
- Logout (инвалидация refresh token) — Phase 8 Hardening
- Rate limiting на auth endpoints — Phase 8 Hardening
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| AUTH-01 | Пользователь авторизуется через Telegram Login Widget → получает JWT access token (15 мин) и refresh token (30 дней) | Telegram HMAC verification algo documented; PyJWT 2.12.1 encode/decode API verified; SQLAlchemy upsert pattern confirmed |
| AUTH-02 | Пользователь обновляет access token через refresh token | PyJWT decode pattern; DB lookup by jti; issue new access_token pattern documented |
| AUTH-03 | Все API эндпоинты требуют валидный Bearer токен | OAuth2PasswordBearer + get_current_user Dependency pattern verified from official FastAPI docs |
</phase_requirements>

---

## Summary

Phase 2 реализует три взаимосвязанных компонента: верификацию Telegram Login Widget payload через HMAC-SHA256, выдачу JWT токенов через PyJWT 2.12.1, и FastAPI Dependency `get_current_user`, которую все последующие фазы будут подключать через `Depends`. Все три алгоритма хорошо документированы — никакой неопределённости по ключевым вопросам нет.

Критически важный нюанс: `User.id` в существующей схеме — это `BigInteger` со значением Telegram `user_id` (не auto-generated UUID). Это означает что JWT `sub` должен быть строкой из Telegram ID, а upsert использует `id` как conflict target. Refresh token таблица добавляется отдельной миграцией `0002_add_refresh_tokens.py` поверх `0001_initial_schema.py`.

PyJWT 2.12.1 — уже выбранная библиотека (подтверждена PyPI, март 2026). `encode()` принимает `algorithm` (str), `decode()` принимает `algorithms` (list). Для protect-endpoints используется `OAuth2PasswordBearer` + `Depends(get_current_user)`.

**Primary recommendation:** Реализовать в трёх файлах — `app/services/auth.py` (бизнес-логика), `app/api/auth.py` (эндпоинты), `app/api/deps.py` (get_current_user dependency). Alembic миграция создаётся вручную (не autogenerate) для предсказуемости.

---

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| PyJWT | 2.12.1 | JWT encode/decode | Уже в проекте (STACK.md), единственная неброшенная альтернатива python-jose |
| FastAPI security | 0.135.3 | OAuth2PasswordBearer | Встроен в FastAPI, нет доп. зависимостей |
| Python stdlib hmac | built-in | HMAC-SHA256 верификация | Locked decision — без сторонних библиотек |
| Python stdlib hashlib | built-in | SHA256 для secret key | Locked decision |
| SQLAlchemy (postgresql dialect) | 2.0.49 | Upsert via insert().on_conflict_do_update() | Уже в проекте |
| Alembic | 1.18.4 | Миграция refresh_tokens | Уже в проекте |
| Pydantic v2 | встроен в FastAPI | Auth request/response схемы | Уже в проекте |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pytest-asyncio | 1.3.0 | Async тесты auth | Уже в pyproject.toml с `asyncio_mode = "auto"` |
| httpx | 0.28.1 | AsyncClient для интеграционных тестов | Уже в проекте |

### Installation

Новых пакетов не требуется — всё уже в requirements. Проверить что `PyJWT` присутствует:

```bash
pip install PyJWT==2.12.1
```

---

## Architecture Patterns

### Recommended Project Structure

```
app/
├── api/
│   ├── auth.py          # POST /auth/telegram, POST /auth/refresh
│   ├── deps.py          # get_current_user dependency (shared across phases)
│   └── health.py        # существует из Phase 1
├── models/
│   ├── refresh_token.py # RefreshToken ORM model
│   └── user.py          # уже существует
├── schemas/
│   ├── auth.py          # TelegramAuthRequest, AuthResponse, RefreshRequest
│   └── user.py          # UserResponse (создаётся здесь)
├── services/
│   └── auth.py          # verify_telegram_hash(), create_tokens(), verify_token()
├── config.py            # добавить jwt_secret, jwt_algorithm, token expiry
└── main.py              # include_router(auth_router, prefix="/auth")

alembic/versions/
├── 0001_initial_schema.py  # существует
└── 0002_add_refresh_tokens.py  # создаётся в Phase 2
```

### Pattern 1: Telegram HMAC-SHA256 Verification

**What:** Верификация данных Telegram Login Widget через HMAC. Telegram отправляет hash вместе с данными пользователя — нужно воспроизвести вычисление hash на сервере и сравнить.

**Exact algorithm (HIGH confidence — official Telegram docs):**
1. Убрать поле `hash` из полученных данных
2. Отсортировать оставшиеся поля alphabetically
3. Склеить в формате `key=value\nkey=value` (separator: `\n`)
4. `secret_key = hashlib.sha256(bot_token.encode()).digest()` — DIGEST (bytes), не hexdigest
5. `hmac_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()`
6. `hmac.compare_digest(hmac_hash, received_hash)` — timing-safe сравнение

**Fields that come from Telegram:** `id`, `first_name`, `last_name` (optional), `username` (optional), `photo_url` (optional), `auth_date`, `hash`

**auth_date check:** `time.time() - auth_date < 86400` — иначе 401

```python
# Source: https://core.telegram.org/widgets/login (verified)
import hashlib
import hmac
import time

def verify_telegram_auth(data: dict, bot_token: str) -> bool:
    received_hash = data.pop("hash")
    auth_date = int(data.get("auth_date", 0))

    if time.time() - auth_date > 86400:
        return False  # данные устарели

    check_string = "\n".join(
        f"{k}={v}"
        for k, v in sorted(data.items())
        if v is not None
    )
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    computed = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, received_hash)
```

**Pitfall:** None-значения опциональных полей (last_name, username, photo_url) не включаются в data_check_string — проверяй `if v is not None`.

### Pattern 2: PyJWT 2.12.1 Encode/Decode

**Encode API:**
```python
# Source: PyJWT 2.12.1 official docs (pyjwt.readthedocs.io)
import jwt
from datetime import datetime, timedelta, timezone

def create_access_token(user_id: int, telegram_id: int) -> str:
    payload = {
        "sub": str(user_id),           # required: subject
        "telegram_id": telegram_id,    # custom claim
        "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    # Note: algorithm= (singular) для encode

def create_refresh_token(user_id: int, jti: str) -> str:
    payload = {
        "sub": str(user_id),
        "jti": jti,                    # JWT ID — для инвалидации через БД
        "type": "refresh",
        "exp": datetime.now(timezone.utc) + timedelta(days=30),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
```

**Decode API:**
```python
# Source: PyJWT 2.12.1 official docs
from jwt.exceptions import InvalidTokenError, ExpiredSignatureError

def decode_token(token: str) -> dict:
    # algorithms= (plural, list) для decode — обязательно!
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    return payload
    # Автоматически валидирует exp claim — бросает ExpiredSignatureError если истёк
```

**Critical distinction:** `encode()` принимает `algorithm=` (str), `decode()` принимает `algorithms=` (list). Перепутать — SyntaxError или TypeError.

### Pattern 3: FastAPI OAuth2PasswordBearer + get_current_user

**What:** Dependency injection — все защищённые endpoint'ы получают `current_user: User = Depends(get_current_user)`.

```python
# app/api/deps.py — Source: FastAPI official docs (fastapi.tiangolo.com/tutorial/security/oauth2-jwt/)
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from jwt.exceptions import InvalidTokenError

from app.database import get_session
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/telegram")

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        if payload.get("type") != "access":
            raise credentials_exception
        user_id = int(payload["sub"])
    except (InvalidTokenError, KeyError, ValueError):
        raise credentials_exception

    user = await session.get(User, user_id)
    if user is None:
        raise credentials_exception
    return user
```

**Usage на защищённом endpoint:**
```python
from app.api.deps import get_current_user

@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user
```

### Pattern 4: SQLAlchemy Upsert (User при логине)

**What:** При первом логине через Telegram — INSERT нового пользователя. При повторном — UPDATE (last_name, username, photo_url могли измениться).

Ключевой факт: `User.id` — это Telegram `user_id` (BigInteger, NOT auto-generated). Conflict target = `id`.

```python
# Source: SQLAlchemy 2.0 docs + verified WebSearch (docs.sqlalchemy.org/en/20/dialects/postgresql.html)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.models.user import User

async def upsert_user(session: AsyncSession, tg_data: dict) -> User:
    stmt = (
        pg_insert(User)
        .values(
            id=int(tg_data["id"]),          # Telegram user_id → PK
            username=tg_data.get("username"),
            first_name=tg_data["first_name"],
            last_name=tg_data.get("last_name"),
            photo_url=tg_data.get("photo_url"),
        )
        .on_conflict_do_update(
            index_elements=["id"],           # conflict on PK
            set_={
                "username": tg_data.get("username"),
                "first_name": tg_data["first_name"],
                "last_name": tg_data.get("last_name"),
                "photo_url": tg_data.get("photo_url"),
            },
        )
        .returning(User)
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.scalars().one()
```

### Pattern 5: Refresh Token DB Model

**What:** Хранение refresh tokens в БД для возможности инвалидации (logout — Phase 8).

```python
# app/models/refresh_token.py
import uuid
from datetime import datetime
from sqlalchemy import BigInteger, Text, TIMESTAMP, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID
from app.models.base import Base

class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    jti: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # JWT ID
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default="now()", nullable=False
    )
```

### Anti-Patterns to Avoid

- **`algorithm=` в decode()` вместо `algorithms=`:** PyJWT бросит TypeError. Decode всегда принимает list.
- **Не проверять `type` claim:** refresh token нельзя использовать как access token — всегда проверять `payload["type"] == "access"` в get_current_user.
- **Включать None поля в data_check_string:** Telegram не включает отсутствующие поля — filter `if v is not None`.
- **hexdigest() как secret_key:** Нужен `.digest()` (bytes), не `.hexdigest()` (str) — секретный ключ должен быть bytes.
- **from sqlalchemy import insert (не postgresql):** Для `on_conflict_do_update` нужен `from sqlalchemy.dialects.postgresql import insert`.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Timing-safe comparison | `hash_a == hash_b` | `hmac.compare_digest()` | Защита от timing attack — разница в несколько наносекунд позволяет брутфорсить hash |
| JWT expiry validation | Проверять `exp` вручную | PyJWT `decode()` с `exp` в payload | PyJWT автоматически бросает `ExpiredSignatureError` при истёкшем токене |
| Bearer token extraction | Парсить `Authorization` header вручную | `OAuth2PasswordBearer` | Обрабатывает edge cases, генерирует OpenAPI схему |
| Token rotation (refresh) | Custom state machine | JWT `jti` + DB lookup | jti уже встроен в стандарт JWT (RFC 7519) |

**Key insight:** Stdlib `hmac` + `hashlib` — правильный выбор для этого домена. Никакой crypto-логики за пределами этих двух модулей.

---

## Common Pitfalls

### Pitfall 1: encode vs decode parameter name asymmetry

**What goes wrong:** `jwt.decode(token, secret, algorithm="HS256")` — не работает (TypeError).
**Why it happens:** PyJWT API асимметричен: `encode(algorithm=str)`, `decode(algorithms=list)`.
**How to avoid:** В `decode()` всегда передавать список: `algorithms=["HS256"]`.
**Warning signs:** `TypeError: decode() got an unexpected keyword argument 'algorithm'`.

### Pitfall 2: HMAC secret key — digest vs hexdigest

**What goes wrong:** `secret_key = hashlib.sha256(token.encode()).hexdigest()` → HMAC вычисляется неверно, верификация всегда провалится.
**Why it happens:** `hexdigest()` возвращает str (hex-encoded), нужен bytes. HMAC ожидает bytes как ключ.
**How to avoid:** `hashlib.sha256(token.encode()).digest()` — обязательно `.digest()`.
**Warning signs:** Валидные Telegram payload'ы отклоняются с 401.

### Pitfall 3: User.id — это telegram_id, не auto UUID

**What goes wrong:** JWT payload `sub` содержит UUID когда ожидается BigInteger telegram_id.
**Why it happens:** Код написан в предположении что `id` — автогенерируемый UUID (как в других моделях), но в `user.py` это явно Telegram user_id.
**How to avoid:** `sub = str(user.id)` где `user.id` — BigInteger. При decode: `user_id = int(payload["sub"])`.
**Warning signs:** `session.get(User, user_id)` возвращает None для существующих пользователей.

### Pitfall 4: Refresh token reuse без проверки типа

**What goes wrong:** Refresh token принят как access token (или наоборот) — любой кто перехватил refresh token получает доступ к API.
**Why it happens:** `get_current_user` декодирует токен но не проверяет `type` claim.
**How to avoid:** В `get_current_user` добавить `if payload.get("type") != "access": raise credentials_exception`. В `/auth/refresh` проверять `type == "refresh"`.

### Pitfall 5: on_conflict_do_update — нужен postgresql import, не generic

**What goes wrong:** `from sqlalchemy import insert` → нет метода `on_conflict_do_update`.
**Why it happens:** Generic insert не содержит PostgreSQL-specific методы.
**How to avoid:** `from sqlalchemy.dialects.postgresql import insert as pg_insert`.

### Pitfall 6: OAuth2PasswordBearer tokenUrl

**What goes wrong:** Swagger UI пытается логиниться через `/auth/telegram` как form (username/password), что несовместимо с Telegram Widget.
**Why it happens:** `tokenUrl` в OAuth2PasswordBearer указывает endpoint для Swagger authorize — это декоративно, не функционально.
**How to avoid:** Задать `tokenUrl="/auth/telegram"` — это только для OpenAPI схемы, не влияет на runtime поведение. Пользователи будут получать токен вне Swagger.

---

## Code Examples

### Полная схема POST /auth/telegram flow

```python
# 1. Pydantic schema (app/schemas/auth.py)
from pydantic import BaseModel
from typing import Optional

class TelegramAuthRequest(BaseModel):
    id: int
    first_name: str
    last_name: Optional[str] = None
    username: Optional[str] = None
    photo_url: Optional[str] = None
    auth_date: int
    hash: str

class UserInfo(BaseModel):
    id: int          # telegram_id (= User.id)
    username: Optional[str]

class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 900   # 15 * 60

class RefreshRequest(BaseModel):
    refresh_token: str
```

### Alembic миграция 0002

```python
# alembic/versions/0002_add_refresh_tokens.py
revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"   # ← ссылка на 0001

def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("jti", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token"),
        sa.UniqueConstraint("jti"),
    )

def downgrade() -> None:
    op.drop_table("refresh_tokens")
```

### Config additions

```python
# app/config.py — добавить в Settings
jwt_secret: str = "change-me-in-production"   # уже есть как jwt_secret
jwt_algorithm: str = "HS256"
access_token_expire_minutes: int = 15
refresh_token_expire_days: int = 30
tg_bot_token: str = ""   # нужен для HMAC верификации Telegram Login
```

**Важно:** `jwt_secret` уже существует в `config.py`. Добавить только недостающие поля. Поле называется `jwt_secret` (не `jwt_secret_key`) — не переименовывать.

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| python-jose | PyJWT 2.12.1 | 2024-2025 (FastAPI docs) | python-jose брошен, CVE, заменён PyJWT |
| `@app.on_event("startup")` | lifespan context manager | FastAPI 0.95+ | уже применено в Phase 1 |
| passlib для хэширования | bcrypt напрямую | Python 3.11+ | passlib deprecated, но для этого проекта не нужен |
| `jwt.decode(..., options={"verify_exp": False})` | По умолчанию exp проверяется | PyJWT 2.x | Старые гайды могут содержать `options` — не нужно |

**Deprecated/outdated:**
- `from jwt import decode` (старый стиль) → использовать `import jwt; jwt.decode()`
- `python-jose`: не устанавливать, не упоминать в requirements

---

## Open Questions

1. **`tg_bot_token` в config — какое имя env var?**
   - Что мы знаем: нужен токен бота для Telegram HMAC верификации
   - Что неясно: коллизия с `tg_api_id`/`tg_api_hash` (для MTProto) vs bot_token (для Login Widget)
   - Recommendation: добавить `tg_bot_token: str = ""` отдельно от MTProto credentials — они для разных целей

2. **`get_session` vs `async_session` — naming inconsistency**
   - Что мы знаем: в `database.py` dependency называется `get_session()`, в `code_context` упомянута `get_db`
   - Что неясно: нужно ли переименовать для consistency
   - Recommendation: использовать существующее `get_session` без переименования — Phase 2 не рефакторит Phase 1

---

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 8.x + pytest-asyncio 1.3.0 |
| Config file | `pyproject.toml` (`asyncio_mode = "auto"`) |
| Quick run command | `pytest tests/test_auth.py -x -q` |
| Full suite command | `pytest tests/ -x -q` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| AUTH-01 | Валидный Telegram payload → 200 + access_token + refresh_token | unit | `pytest tests/test_auth.py::test_telegram_auth_success -x` | ❌ Wave 0 |
| AUTH-01 | Поддельный HMAC hash → 401 | unit | `pytest tests/test_auth.py::test_telegram_auth_invalid_hash -x` | ❌ Wave 0 |
| AUTH-01 | Устаревший auth_date (>24ч) → 401 | unit | `pytest tests/test_auth.py::test_telegram_auth_expired_date -x` | ❌ Wave 0 |
| AUTH-01 | Первый логин создаёт нового User в БД | integration | `pytest tests/test_auth.py::test_user_created_on_first_login -x` | ❌ Wave 0 |
| AUTH-01 | Повторный логин обновляет User (upsert) | integration | `pytest tests/test_auth.py::test_user_upserted_on_second_login -x` | ❌ Wave 0 |
| AUTH-02 | Валидный refresh_token → 200 + новый access_token | unit | `pytest tests/test_auth.py::test_refresh_token_success -x` | ❌ Wave 0 |
| AUTH-02 | Невалидный refresh_token → 401 | unit | `pytest tests/test_auth.py::test_refresh_token_invalid -x` | ❌ Wave 0 |
| AUTH-02 | Access token как refresh → 401 | unit | `pytest tests/test_auth.py::test_refresh_wrong_token_type -x` | ❌ Wave 0 |
| AUTH-03 | Запрос без Bearer → 401 | unit | `pytest tests/test_auth.py::test_protected_no_token -x` | ❌ Wave 0 |
| AUTH-03 | Запрос с валидным Bearer → 200 | unit | `pytest tests/test_auth.py::test_protected_valid_token -x` | ❌ Wave 0 |
| AUTH-03 | Просроченный access token → 401 | unit | `pytest tests/test_auth.py::test_protected_expired_token -x` | ❌ Wave 0 |
| AUTH-03 | Refresh token вместо access → 401 | unit | `pytest tests/test_auth.py::test_protected_refresh_as_access -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `pytest tests/test_auth.py -x -q`
- **Per wave merge:** `pytest tests/ -x -q`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps

- [ ] `tests/test_auth.py` — все тесты AUTH-01..03 (12 кейсов)
- [ ] `tests/conftest.py` — фикстуры: async test client, test DB session, mock Telegram payload builder
- [ ] `tests/__init__.py` — пустой файл для pytest discovery

**Fixtures needed in conftest.py:**
```python
# tests/conftest.py skeleton
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app

@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

@pytest.fixture
def valid_tg_payload(settings):
    """Build valid Telegram Login payload with correct HMAC."""
    # ...generate real HMAC for test bot token
```

---

## Sources

### Primary (HIGH confidence)
- [Telegram Login Widget official docs](https://core.telegram.org/widgets/login) — HMAC algorithm, data_check_string format, auth_date validity
- [FastAPI official docs: OAuth2 JWT](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/) — OAuth2PasswordBearer, get_current_user pattern, HTTPException 401
- [PyJWT 2.12.1 PyPI](https://pypi.org/project/PyJWT/) — версия подтверждена (март 2026)
- [SQLAlchemy 2.0 PostgreSQL dialect docs](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html) — insert().on_conflict_do_update()

### Secondary (MEDIUM confidence)
- [WebSearch: PyJWT 2.12 encode/decode API](https://pyjwt.readthedocs.io/en/stable/algorithms.html) — подтверждён через официальные docs ReadTheDocs
- [WebSearch: SQLAlchemy async upsert patterns 2025](https://levelup.gitconnected.com/how-to-perform-upsert-actions-for-postgresql-with-sqlalchemy-orm-27af19242495) — cross-verified с официальной документацией

### Tertiary (LOW confidence)
- Нет — все критические утверждения верифицированы через первичные источники

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — все библиотеки уже в проекте, версии верифицированы PyPI
- Architecture: HIGH — Telegram HMAC алгоритм из официальных docs; PyJWT API из официальной документации; FastAPI pattern из официального tutorial
- Pitfalls: HIGH — encode/decode asymmetry верифицирована через WebSearch + official docs; остальные из прямого анализа существующего кода

**Research date:** 2026-04-06
**Valid until:** 2026-05-06 (стабильные библиотеки; PyJWT + SQLAlchemy редко меняют API)
