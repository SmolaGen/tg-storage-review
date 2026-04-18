---
phase: 02-auth
plan: 01
subsystem: auth
tags: [jwt, telegram, hmac-sha256, pyjwt, fastapi, sqlalchemy, alembic]

requires:
  - phase: 01-foundation
    provides: User model (BigInteger telegram_id PK), Base, async_session, get_session dependency

provides:
  - HMAC-SHA256 Telegram Login Widget verification (stdlib only)
  - JWT access tokens (15 min, HS256) via PyJWT
  - JWT refresh tokens (30 days) with jti UUID for revocation
  - RefreshToken SQLAlchemy model + Alembic 0002 migration
  - get_current_user FastAPI Dependency (app/api/deps.py)
  - POST /auth/telegram — open registration via upsert
  - POST /auth/refresh — exchange refresh token for new access token
  - GET /auth/me — current user endpoint (protected)
affects: [03-storage, 04-upload, 05-download, 06-files, 07-bot, 08-hardening]

tech-stack:
  added: [PyJWT, aiosqlite (tests)]
  patterns: [OAuth2PasswordBearer dependency injection, pg_insert upsert, SQLite for test isolation]

key-files:
  created:
    - app/services/auth.py
    - app/api/auth.py
    - app/api/deps.py
    - app/models/refresh_token.py
    - app/schemas/auth.py
    - app/schemas/user.py
    - alembic/versions/0002_add_refresh_tokens.py
    - tests/test_auth.py
    - tests/conftest.py
  modified:
    - app/config.py
    - app/models/__init__.py
    - app/main.py

key-decisions:
  - "HMAC secret uses hashlib.sha256(bot_token).digest() not .hexdigest() — raw bytes required"
  - "PyJWT encode() uses algorithm= singular str, decode() uses algorithms= plural list"
  - "User.id IS telegram_id (BigInteger), not auto UUID"
  - "upsert via sqlalchemy.dialects.postgresql.insert with on_conflict_do_update"
  - "Tests use SQLite+aiosqlite for full isolation — ORM create_all bypasses PostgreSQL-specific migration SQL"

patterns-established:
  - "get_current_user in app/api/deps.py — import in all subsequent endpoint files"
  - "OAuth2PasswordBearer with tokenUrl=/auth/telegram"
  - "HTTPException 401 with WWW-Authenticate: Bearer header for auth failures"
  - "Token type check (payload['type'] != 'access') prevents refresh token as access token"

requirements-completed: [AUTH-01, AUTH-02, AUTH-03]

duration: 15min
completed: 2026-04-06
---

# Phase 02-01: Auth Summary

**Telegram HMAC login + HS256 JWT access/refresh tokens + get_current_user Bearer middleware — all API endpoints can now be protected**

## Performance

- **Duration:** ~15 min
- **Completed:** 2026-04-06
- **Tasks:** 2
- **Files modified:** 15

## Accomplishments
- HMAC-SHA256 Telegram Login Widget verification with 24h auth_date timeout
- JWT access (15 min) + refresh (30 days) tokens with PyJWT; jti UUID column for future revocation
- FastAPI `get_current_user` dependency in `app/api/deps.py` — reusable across all phases
- Alembic migration 0002 adding `refresh_tokens` table on top of 0001
- 15 tests pass (unit + integration on SQLite aiosqlite)

## Task Commits

1. **Task 1: AuthService + RefreshToken + schemas + tests** - `57d86e0`
2. **Task 2: Auth Router + main.py wiring** - `1ba81f0`

## Files Created/Modified
- `app/services/auth.py` — verify_telegram_auth, create_access_token, create_refresh_token, decode_token, upsert_user
- `app/api/auth.py` — POST /telegram, POST /refresh, GET /me
- `app/api/deps.py` — get_current_user, oauth2_scheme
- `app/models/refresh_token.py` — RefreshToken ORM model with jti unique
- `app/schemas/auth.py` — TelegramAuthRequest, AuthResponse, RefreshRequest, RefreshResponse
- `app/schemas/user.py` — UserInfo
- `alembic/versions/0002_add_refresh_tokens.py` — down_revision=a1b2c3d4e5f6
- `tests/test_auth.py` — 15 test cases
- `tests/conftest.py` — SQLite fixtures, valid_tg_payload helper
- `app/config.py` — added jwt_algorithm, access_token_expire_minutes, refresh_token_expire_days, tg_bot_token
- `app/main.py` — include_router(auth_router, prefix="/auth")

## Decisions Made
- Used `.digest()` not `.hexdigest()` for HMAC secret key — raw bytes is the correct Telegram spec
- PyJWT API asymmetry respected: `encode(algorithm=)` vs `decode(algorithms=[])`
- SQLite+aiosqlite for test isolation; `pg_insert` upsert is PostgreSQL-only but only runs in production

## Deviations from Plan
None — plan executed exactly as written.

## Issues Encountered
- Executor agent returned early (classifyHandoffIfNeeded bug) after creating models/tests but before services/schemas/router. Resumed manually — all files created and committed correctly.

## Next Phase Readiness
- `get_current_user` ready in `app/api/deps.py` — Phase 3 (Storage) imports it immediately
- `get_session` dependency pattern established — all subsequent phases follow the same pattern

---
*Phase: 02-auth*
*Completed: 2026-04-06*
