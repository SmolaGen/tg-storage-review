# Phase 2: Auth - Context

**Gathered:** 2026-04-06
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 2 delivers the authentication layer: Telegram Login Widget verification, JWT issue/refresh, and Bearer token middleware. After this phase every API endpoint can be protected. No file upload/download logic — pure auth plumbing.

</domain>

<decisions>
## Implementation Decisions

### JWT Configuration
- Алгоритм подписи: HS256 (симметричный ключ из env var `JWT_SECRET_KEY`)
- Access token lifetime: 15 минут
- Refresh token lifetime: 30 дней
- Refresh tokens хранятся в БД (таблица `refresh_tokens` — UUID id, user_id FK, token TEXT, expires_at, created_at) — возможность инвалидации при logout

### Telegram Auth Security
- Таймаут валидности Telegram Login данных: 86400 секунд (24ч) — проверять `auth_date` из payload
- Открытая регистрация — любой Telegram пользователь может войти
- Автосоздание user при первом логине: upsert по `telegram_id` (INSERT ON CONFLICT DO UPDATE)
- HMAC-SHA256 верификация через stdlib `hmac` + `hashlib` (без сторонних библиотек)

### API Design
- `POST /auth/telegram` response: `{access_token, refresh_token, token_type: "bearer", expires_in: 900, user: {id, telegram_id, username}}`
- Bearer token: стандартный заголовок `Authorization: Bearer <token>` (FastAPI OAuth2PasswordBearer совместим)
- 401 с `{"detail": "Invalid or expired token"}` при невалидном/просроченном токене
- FastAPI Dependency `get_current_user` — переиспользуется во всех последующих фазах

### Claude's Discretion
- Структура файлов: `app/services/auth.py`, `app/api/auth.py`, `app/models/refresh_token.py`
- Alembic миграция для таблицы `refresh_tokens` (новая миграция поверх Phase 1)
- Тесты для HMAC верификации и JWT issue/verify

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `app/models/base.py` — Base, UUIDMixin, TimestampMixin — использовать для RefreshToken модели
- `app/models/user.py` — User модель с `telegram_id` уже существует из Phase 1
- `app/database.py` — `async_session_factory`, `get_db` dependency — использовать в auth endpoints
- `app/config.py` — `Settings` BaseSettings — добавить `jwt_secret_key`, `jwt_algorithm`, `access_token_expire_minutes`, `refresh_token_expire_days`
- `app/main.py` — lifespan pattern, роутер включается через `app.include_router()`

### Established Patterns
- Dependency injection через FastAPI `Depends`
- Async SQLAlchemy: `async with async_session_factory() as session`
- Pydantic schemas в `app/schemas/` (ещё не созданы — Phase 2 создаёт первые)

### Integration Points
- `app/main.py` → `app.include_router(auth_router, prefix="/auth", tags=["auth"])`
- `get_current_user` dependency будет импортироваться из `app/api/deps.py` во всех последующих фазах
- Alembic: новая миграция `0002_add_refresh_tokens.py`

</code_context>

<specifics>
## Specific Ideas

- HMAC верификация: `secret = hashlib.sha256(bot_token.encode()).digest()` → `hmac.new(secret, data_check_string.encode(), hashlib.sha256)`
- JWT payload: `{"sub": str(user.id), "telegram_id": user.telegram_id, "exp": ..., "type": "access"}`
- Refresh token payload: `{"sub": str(user.id), "jti": str(uuid4()), "exp": ..., "type": "refresh"}` — `jti` для инвалидации по БД записи

</specifics>

<deferred>
## Deferred Ideas

- Logout (инвалидация refresh token) — Phase 8 Hardening
- Rate limiting на auth endpoints — Phase 8 Hardening

</deferred>
