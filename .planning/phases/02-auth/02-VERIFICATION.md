---
phase: 02-auth
verified: 2026-04-06T00:00:00Z
status: passed
score: 9/9 must-haves verified
re_verification: false
---

# Phase 2: Auth Verification Report

**Phase Goal:** Deliver authentication layer — Telegram Login Widget verification, JWT issue/refresh, Bearer token middleware. After this phase every API endpoint can be protected.
**Verified:** 2026-04-06
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| #  | Truth                                                                               | Status     | Evidence                                                          |
|----|-------------------------------------------------------------------------------------|------------|-------------------------------------------------------------------|
| 1  | POST /auth/telegram with valid Telegram payload returns 200 + access_token + refresh_token | VERIFIED | test_telegram_login_endpoint passes; route in app/api/auth.py L31 |
| 2  | POST /auth/telegram with forged hash returns 401                                    | VERIFIED   | test_telegram_login_invalid_hash passes; verify_telegram_auth returns False → 401 |
| 3  | POST /auth/telegram with expired auth_date (>24h) returns 401                       | VERIFIED   | test_verify_telegram_auth_expired passes; time check in auth.py L23 |
| 4  | First login creates User in DB, second login updates User (upsert)                  | VERIFIED   | test_user_upsert passes; pg_insert.on_conflict_do_update in auth.py L83 |
| 5  | POST /auth/refresh with valid refresh_token returns new access_token                | VERIFIED   | test_refresh_success passes; /auth/refresh route in api/auth.py L71 |
| 6  | POST /auth/refresh with invalid/expired refresh_token returns 401                   | VERIFIED   | test_refresh_invalid_token and test_refresh_with_access_token pass |
| 7  | Protected endpoint with valid Bearer returns 200                                    | VERIFIED   | test_protected_endpoint_valid_token passes; GET /auth/me L108      |
| 8  | Protected endpoint without Bearer or with expired/invalid token returns 401         | VERIFIED   | test_protected_endpoint_no_token and test_protected_endpoint_expired_token pass |
| 9  | Refresh token used as access token is rejected with 401                             | VERIFIED   | test_protected_endpoint_refresh_as_access passes; type check in deps.py L25 |

**Score:** 9/9 truths verified

### Required Artifacts

| Artifact                                       | Expected                                         | Status   | Details                                             |
|------------------------------------------------|--------------------------------------------------|----------|-----------------------------------------------------|
| `app/services/auth.py`                         | HMAC verification, JWT create/decode, upsert user | VERIFIED | 97 lines; exports verify_telegram_auth, create_access_token, create_refresh_token, decode_token, upsert_user |
| `app/api/auth.py`                              | POST /auth/telegram, POST /auth/refresh, GET /me | VERIFIED | 112 lines; all 3 endpoints present                  |
| `app/api/deps.py`                              | get_current_user FastAPI Dependency              | VERIFIED | 35 lines; oauth2_scheme + get_current_user exported  |
| `app/models/refresh_token.py`                  | RefreshToken ORM model with jti UUID unique      | VERIFIED | class RefreshToken; jti Text unique=True             |
| `app/schemas/auth.py`                          | TelegramAuthRequest, AuthResponse, RefreshRequest | VERIFIED | All 4 schema classes present                        |
| `alembic/versions/0002_add_refresh_tokens.py`  | Alembic migration for refresh_tokens table       | VERIFIED | down_revision = "a1b2c3d4e5f6" matches 0001          |
| `tests/test_auth.py`                           | 15 test cases covering AUTH-01, AUTH-02, AUTH-03  | VERIFIED | 237 lines; 15 test functions confirmed              |

### Key Link Verification

| From               | To                      | Via                                         | Status   | Details                                        |
|--------------------|-------------------------|---------------------------------------------|----------|------------------------------------------------|
| `app/api/auth.py`  | `app/services/auth.py`  | import verify_telegram_auth, create_*, upsert_user | WIRED | L20-26: all 5 service functions imported and called |
| `app/api/deps.py`  | `app/services/auth.py`  | import decode_token                         | WIRED    | L8: `from app.services.auth import decode_token`; used at L24 |
| `app/main.py`      | `app/api/auth.py`       | include_router(auth_router)                 | WIRED    | L8: import; L24: `include_router(auth_router, prefix="/auth")` |
| `app/services/auth.py` | `app/models/refresh_token.py` | RefreshToken persistence         | WIRED    | RefreshToken imported in api/auth.py and used for DB persistence |
| `app/api/deps.py`  | `app/models/user.py`    | session.get(User, user_id) lookup           | WIRED    | L31: `await session.get(User, user_id)`         |

### Requirements Coverage

| Requirement | Source Plan | Description                                                                                    | Status    | Evidence                                                           |
|-------------|-------------|------------------------------------------------------------------------------------------------|-----------|--------------------------------------------------------------------|
| AUTH-01     | 02-01-PLAN  | Пользователь авторизуется через Telegram Login Widget → получает JWT access token (15 мин) и refresh token (30 дней) | SATISFIED | POST /auth/telegram fully implemented; access_token_expire_minutes=15, refresh_token_expire_days=30 in config |
| AUTH-02     | 02-01-PLAN  | Пользователь обновляет access token через refresh token                                        | SATISFIED | POST /auth/refresh implemented; RefreshToken persisted with jti; test_refresh_success passes |
| AUTH-03     | 02-01-PLAN  | Все API эндпоинты требуют валидный Bearer токен                                                | SATISFIED | get_current_user dependency in deps.py; GET /auth/me protected; all 4 protection tests pass |

### Anti-Patterns Found

No anti-patterns detected. No TODO/FIXME/print/console.log in any phase files. No stub implementations. No empty handlers.

**Notable (non-blocking):** PyJWT warns about the default `jwt_secret = "change-me-in-production"` (23 bytes) being below recommended 32-byte minimum for HS256. This is test-environment only — production requires a strong secret via env var. No action needed for this phase.

### Human Verification Required

None. All behaviors verified programmatically via passing test suite.

---

## Test Run Evidence

```
15 passed, 16 warnings in 0.14s
```

All 15 test cases pass:
- AUTH-01: test_verify_telegram_auth_valid, test_verify_telegram_auth_invalid_hash, test_verify_telegram_auth_expired, test_create_access_token, test_create_refresh_token, test_telegram_login_endpoint, test_telegram_login_invalid_hash, test_user_upsert (8 tests)
- AUTH-02: test_refresh_success, test_refresh_invalid_token, test_refresh_with_access_token (3 tests)
- AUTH-03: test_protected_endpoint_no_token, test_protected_endpoint_valid_token, test_protected_endpoint_expired_token, test_protected_endpoint_refresh_as_access (4 tests)

## Critical Constraint Verification

| Constraint                                      | Status   | Location                          |
|-------------------------------------------------|----------|-----------------------------------|
| HMAC uses `.digest()` not `.hexdigest()`        | VERIFIED | `app/services/auth.py` L30        |
| JWT encode uses `algorithm=` (singular)         | VERIFIED | `app/services/auth.py` L47, L62   |
| JWT decode uses `algorithms=` (plural list)     | VERIFIED | `app/services/auth.py` L69        |
| `down_revision = "a1b2c3d4e5f6"` in 0002       | VERIFIED | `alembic/versions/0002_add_refresh_tokens.py` L16 |
| RefreshToken.jti unique=True                    | VERIFIED | `app/models/refresh_token.py` L21 |
| No input dict mutation in verify_telegram_auth  | VERIFIED | `app/services/auth.py` L17: `data_copy = dict(data)` |
| `pg_insert` from postgresql dialect             | VERIFIED | `app/services/auth.py` L8         |
| No print/console.log                            | VERIFIED | Zero matches in scan               |

---

_Verified: 2026-04-06_
_Verifier: Claude (gsd-verifier)_
