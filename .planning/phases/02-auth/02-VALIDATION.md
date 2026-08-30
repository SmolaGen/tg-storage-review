---
phase: 2
slug: auth
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-04-06
---

# Phase 2 — Validation Strategy

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x + pytest-asyncio 1.3.0 + httpx 0.28.1 |
| **Config file** | `pyproject.toml` (asyncio_mode = "auto", from Phase 1) |
| **Quick run command** | `pytest tests/test_auth.py -x -q` |
| **Full suite command** | `pytest tests/ -v --tb=short` |
| **Estimated runtime** | ~10 seconds |

## Sampling Rate

- **After every task commit:** `pytest tests/test_auth.py -x -q`
- **After every plan wave:** `pytest tests/ -v --tb=short`
- **Before `/gsd:verify-work`:** Full suite must be green

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 02-01-T1 | 02-01 | 1 | AUTH-01 | unit | `pytest tests/test_auth.py::test_verify_telegram_hash -x` | ❌ W0 | ⬜ pending |
| 02-01-T2 | 02-01 | 1 | AUTH-01, AUTH-02 | integration | `pytest tests/test_auth.py::test_login_endpoint -x` | ❌ W0 | ⬜ pending |
| 02-01-T3 | 02-01 | 1 | AUTH-02, AUTH-03 | integration | `pytest tests/test_auth.py::test_refresh_endpoint -x` | ❌ W0 | ⬜ pending |
| 02-01-T4 | 02-01 | 1 | AUTH-02 | integration | `pytest tests/test_auth.py::test_bearer_middleware -x` | ❌ W0 | ⬜ pending |

## Wave 0 Requirements

- [ ] `tests/test_auth.py` — test stubs for AUTH-01, AUTH-02, AUTH-03
- [ ] `tests/conftest.py` — обновить: добавить фикстуры `auth_client`, `test_user`, `valid_tg_payload`

## Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| AUTH-01 | `POST /auth/telegram` с валидным payload → 200 + tokens | integration | `pytest tests/test_auth.py::test_login_endpoint -x` | ❌ Wave 0 |
| AUTH-01 | HMAC-SHA256 верификация отклоняет поддельный payload → 401 | unit | `pytest tests/test_auth.py::test_verify_telegram_hash -x` | ❌ Wave 0 |
| AUTH-02 | Bearer токен на защищённом endpoint → 200; без токена → 401 | integration | `pytest tests/test_auth.py::test_bearer_middleware -x` | ❌ Wave 0 |
| AUTH-03 | `POST /auth/refresh` с валидным refresh_token → новый access_token | integration | `pytest tests/test_auth.py::test_refresh_endpoint -x` | ❌ Wave 0 |

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Telegram Login Widget в браузере | AUTH-01 | Требует реального Telegram бота | Открыть тестовую HTML страницу, войти через Telegram |
