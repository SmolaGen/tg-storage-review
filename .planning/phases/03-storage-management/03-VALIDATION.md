---
phase: 3
slug: storage-management
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-04-06
---

# Phase 3 — Validation Strategy

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (asyncio_mode="auto", из Phase 2) |
| **Config file** | `pyproject.toml` (уже настроен) |
| **Quick run command** | `pytest tests/test_storage.py -x -q` |
| **Full suite command** | `pytest tests/ -q` |
| **Estimated runtime** | ~10 seconds |

## Sampling Rate

- **After every task commit:** `pytest tests/test_storage.py -x -q`
- **After every plan wave:** `pytest tests/ -q`
- **Before `/gsd:verify-work`:** Full suite must be green

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 03-01-T1 | 03-01 | 1 | STOR-02, STOR-03 | unit (mock) | `pytest tests/test_storage.py::test_validate_worker_invalid_token -x` | ❌ W0 | ⬜ pending |
| 03-01-T2 | 03-01 | 1 | STOR-02, STOR-03 | unit (mock) | `pytest tests/test_storage.py::test_validate_worker_no_channel_access -x` | ❌ W0 | ⬜ pending |
| 03-02-T1 | 03-02 | 2 | STOR-01 | integration | `pytest tests/test_storage.py::test_create_storage -x` | ❌ W0 | ⬜ pending |
| 03-02-T2 | 03-02 | 2 | STOR-01 | integration | `pytest tests/test_storage.py::test_list_storages_isolation -x` | ❌ W0 | ⬜ pending |
| 03-02-T3 | 03-02 | 2 | STOR-02 | integration | `pytest tests/test_storage.py::test_add_worker_valid -x` | ❌ W0 | ⬜ pending |
| 03-02-T4 | 03-02 | 2 | STOR-02 | integration | `pytest tests/test_storage.py::test_add_worker_wrong_owner -x` | ❌ W0 | ⬜ pending |
| 03-02-T5 | 03-02 | 2 | STOR-03 | unit (mock) | `pytest tests/test_storage.py::test_add_worker_invalid_token -x` | ❌ W0 | ⬜ pending |
| 03-02-T6 | 03-02 | 2 | STOR-03 | unit (mock) | `pytest tests/test_storage.py::test_add_worker_no_channel_access -x` | ❌ W0 | ⬜ pending |
| 03-02-T7 | 03-02 | 2 | STOR-04 | integration | `pytest tests/test_storage.py::test_list_storages_with_workers -x` | ❌ W0 | ⬜ pending |

## Wave 0 Requirements

- [ ] `tests/test_storage.py` — стабы для STOR-01, STOR-02, STOR-03, STOR-04
- [ ] `app/state.py` — TelegramPool синглтон (создаётся в 03-01)

## Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| STOR-01 | POST /storages создаёт хранилище в БД | integration | `pytest tests/test_storage.py::test_create_storage -x` | ❌ Wave 0 |
| STOR-01 | POST /storages без токена → 401 | integration | `pytest tests/test_storage.py::test_create_storage_unauthorized -x` | ❌ Wave 0 |
| STOR-01 | GET /storages возвращает только хранилища текущего пользователя | integration | `pytest tests/test_storage.py::test_list_storages_isolation -x` | ❌ Wave 0 |
| STOR-02 | POST /storages/{id}/workers с валидным bot_token → 201 | unit (mock) | `pytest tests/test_storage.py::test_add_worker_valid -x` | ❌ Wave 0 |
| STOR-02 | StorageWorker.session_string сохранён в БД | integration | `pytest tests/test_storage.py::test_worker_session_saved -x` | ❌ Wave 0 |
| STOR-02 | POST /storages/{id}/workers для чужого хранилища → 404 | integration | `pytest tests/test_storage.py::test_add_worker_wrong_owner -x` | ❌ Wave 0 |
| STOR-03 | Невалидный bot_token → 400 | unit (mock) | `pytest tests/test_storage.py::test_add_worker_invalid_token -x` | ❌ Wave 0 |
| STOR-03 | Бот без доступа к каналу → 400 | unit (mock) | `pytest tests/test_storage.py::test_add_worker_no_channel_access -x` | ❌ Wave 0 |
| STOR-04 | GET /storages возвращает список с workers | integration | `pytest tests/test_storage.py::test_list_storages_with_workers -x` | ❌ Wave 0 |
| STOR-04 | GET /storages/{id}/workers возвращает workers хранилища | integration | `pytest tests/test_storage.py::test_list_workers -x` | ❌ Wave 0 |

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Реальный bot_token подключается к каналу | STOR-03 | Требует настоящего Telegram бота | Создать бота через BotFather, добавить в приватный канал, вызвать POST /storages/{id}/workers |
