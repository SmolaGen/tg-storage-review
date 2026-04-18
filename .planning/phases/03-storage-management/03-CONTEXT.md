# Phase 3: Storage Management - Context

**Gathered:** 2026-04-06
**Status:** Ready for planning
**Source:** Autonomous research (no discuss-phase)

<domain>
## Phase Boundary

Phase 3 delivers storage registration and bot validation: users create storages (chat_id + name), add bot_tokens as workers, and the service validates each bot_token via Telethon MTProto (get_me() + get_entity(chat_id)). Also delivers TelegramPool singleton that will be reused by Phases 4 and 5 for all upload/download operations. No file operations — pure infrastructure.

</domain>

<decisions>
## Implementation Decisions

### TelegramPool (app/state.py)
- Singleton dict: `dict[UUID, TelegramClient]` + `dict[UUID, asyncio.Semaphore]` (Semaphore(3) per bot)
- Lazy init from DB at lifespan startup — load all active workers with session_string from storage_workers table
- Shutdown: `await client.disconnect()` for all clients in lifespan teardown
- Reconnect pattern: `await client.connect()` with saved StringSession (no new start(bot_token=) needed)

### Bot Token Validation Flow (STOR-03)
- `TelegramClient(StringSession(), api_id, api_hash)` → `await client.start(bot_token=bot_token)`
- `await client.get_me()` — confirms bot is valid
- `await client.get_entity(chat_id)` — confirms bot has access to channel
- `session_string = client.session.save()` — save to DB column storage_workers.session_string
- `await client.disconnect()` after validation
- Catch: `AccessTokenInvalidError` / `AccessTokenExpiredError` → 400 "Invalid or expired bot_token"
- Catch: `ChannelPrivateError` / `ChatAdminRequiredError` → 400 "Bot does not have access to the channel"
- Catch: `UserDeactivatedBanError` → 400 "Bot account is banned"

### IDOR Security
- Always verify `storage.user_id == current_user.id` before adding worker
- Return 404 (not 403) on ownership mismatch — don't leak existence

### API Design
- `POST /storages` → 201, body: `{id, name, chat_id, user_id, created_at}`
- `GET /storages` → 200, list: `[{id, name, chat_id, worker_count}]` (only current user's)
- `POST /storages/{id}/workers` → 201, body: `{id, storage_id, is_active, created_at}` (no bot_token in response)
- `GET /storages/{id}/workers` → 200, list: workers for this storage (ownership verified)

### Claude's Discretion
- File structure: `app/state.py` (TelegramPool), `app/services/storage.py`, `app/api/storages.py`, `app/schemas/storage.py`
- No new Alembic migration needed — all columns exist from Phase 1
- Tests: `tests/test_storage.py` using SQLite+aiosqlite for CRUD, AsyncMock for TelegramClient

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `app/models/storage.py` — Storage model: id UUID, user_id BigInteger FK, name TEXT, chat_id BigInteger, created_at
- `app/models/storage_worker.py` — StorageWorker: id UUID, storage_id FK, bot_token TEXT, session_string TEXT nullable, is_active bool, last_used_at, created_at
- `app/api/deps.py` — `get_current_user` dependency (MUST use for all endpoints)
- `app/database.py` — `get_session`, `async_session`
- `app/config.py` — `tg_api_id`, `tg_api_hash` already present
- `app/main.py` — lifespan pattern for TelegramPool init/shutdown
- `tests/conftest.py` — SQLite+aiosqlite fixtures, client fixture, get_session override

### Integration Points
- `app/main.py` → import TelegramPool from app/state.py, init in lifespan
- `app/main.py` → include_router(storages_router, prefix="/storages", tags=["storages"])
- Phases 4 and 5 will import TelegramPool from app/state.py to get client by worker_id

</code_context>

<specifics>
## Specific Ideas

- TelegramPool: `pool = TelegramPool()` at module level in app/state.py, imported by main.py lifespan and by service layer
- Validation service: `async def validate_and_create_worker(session, storage_id, bot_token, chat_id, user_id) -> StorageWorker`
- Telethon error import: `from telethon.errors import AccessTokenInvalidError, AccessTokenExpiredError, ChannelPrivateError, ChatAdminRequiredError, UserDeactivatedBanError, FloodWaitError`

</specifics>

<deferred>
## Deferred Ideas

- Unique constraint (user_id, chat_id) in Storage — Phase 8 Hardening
- Worker health monitoring — Phase 8
- Rate limiting on storage endpoints — Phase 8

</deferred>
