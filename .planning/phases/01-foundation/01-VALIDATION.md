---
phase: 1
slug: foundation
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-04-06
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x + pytest-asyncio 1.3.0 |
| **Config file** | `pyproject.toml` — `[tool.pytest.ini_options]` (Wave 0 creates it) |
| **Quick run command** | `pytest tests/ -x -q` |
| **Full suite command** | `pytest tests/ -v --tb=short` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/ -x -q`
- **After every plan wave:** Run `pytest tests/ -v --tb=short`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 01-01-T1 | 01-01 | 1 | INFR-01, INFR-02 | integration | `pytest tests/test_migrations.py -x` | ❌ W0 | ⬜ pending |
| 01-01-T2 | 01-01 | 1 | INFR-01, INFR-02 | integration | `pytest tests/test_migrations.py::test_schema_columns -x` | ❌ W0 | ⬜ pending |
| 01-02-T1 | 01-02 | 1 | INFR-01 | integration | `pytest tests/test_health.py -x` | ❌ W0 | ⬜ pending |
| 01-02-T2 | 01-02 | 1 | INFR-01 | integration | `docker compose up -d && curl -f http://localhost:8000/health` | N/A | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/conftest.py` — async engine fixture pointing to test DB, `alembic upgrade head` fixture
- [ ] `tests/test_health.py` — covers INFR-01 health endpoint (`GET /health` → `{"status": "ok"}` 200)
- [ ] `tests/test_migrations.py` — covers INFR-01 (5 tables exist, idempotency) + INFR-02 (columns)
- [ ] `pyproject.toml` — `asyncio_mode = "auto"` + pytest config
- [ ] Install: `pip install pytest pytest-asyncio==1.3.0 httpx==0.28.1`

---

## Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| INFR-01 | `GET /health` returns `{"status": "ok"}` with 200 | integration | `pytest tests/test_health.py -x` | ❌ Wave 0 |
| INFR-01 | All 5 tables exist in DB after `alembic upgrade head` | integration | `pytest tests/test_migrations.py -x` | ❌ Wave 0 |
| INFR-01 | `alembic upgrade head` is idempotent (run twice, no error) | integration | `pytest tests/test_migrations.py::test_idempotent -x` | ❌ Wave 0 |
| INFR-02 | `storage_workers` has `session_string TEXT` column | integration | `pytest tests/test_migrations.py::test_schema_columns -x` | ❌ Wave 0 |
| INFR-02 | `file_chunks` has `position`, `worker_id`, `tg_file_id`, `message_id` | integration | `pytest tests/test_migrations.py::test_schema_columns -x` | ❌ Wave 0 |

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| `docker-compose up` поднимает оба сервиса | INFR-01 | Требует Docker daemon | `docker compose up -d && docker compose ps` — оба сервиса `healthy` |
