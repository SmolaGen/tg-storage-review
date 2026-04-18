# Domain Pitfalls: Telegram-as-Storage with Telethon MTProto

**Domain:** File storage service backed by Telegram (Telethon MTProto, multi-bot, chunked uploads)
**Researched:** 2026-04-06
**Confidence:** HIGH for Telethon-specific items (multiple confirmed sources); MEDIUM for rate limit specifics (Telegram changed algorithm in Bot API 7.0)

---

## Critical Pitfalls

Mistakes that cause data loss, complete rewrites, or production outages.

---

### Pitfall 1: SQLite Session File Locking Under Concurrent Requests

**What goes wrong:** Telethon's default SQLiteSession stores the authorization key and DC information in a `.session` file backed by SQLite. When two or more coroutines (or OS processes) connect using the same session name simultaneously, SQLite raises `sqlite3.OperationalError: database is locked`. The second connection either fails immediately or spins until a timeout.

**Why it happens:** SQLite uses file-level locking. A single `TelegramClient` instance is safe to use from one asyncio event loop — but the moment a second client object opens the same `.session` file (different import, second worker process, or accidental double-instantiation), locking occurs. This is common when running FastAPI with multiple uvicorn workers (`--workers 4`) where each worker process tries to load the same session file.

**Consequences:**
- Upload/download requests fail with `OperationalError` under any concurrency
- Intermittent failures that are hard to reproduce locally (single-worker dev vs. multi-worker prod)
- Silent data corruption if the session's stored state diverges between processes

**Prevention:**
- One `TelegramClient` instance per bot token, created once at application startup (FastAPI `lifespan`), stored in `app.state`
- Each bot gets a **unique** session file name, e.g., `sessions/bot_{token_hash}.session`
- When scaling horizontally (multiple Docker replicas), use `StringSession` stored in the database, not on the filesystem — or enforce single-worker mode and use a task queue for Telegram operations
- Never share a session file between OS processes

**Detection:** `sqlite3.OperationalError: database is locked` in logs; requests failing only under concurrent upload load

**Phase:** Must be resolved in Phase 1 (foundation/session management) before any upload logic is built.

---

### Pitfall 2: file_id Is Permanently Bound to the Uploading Bot Token

**What goes wrong:** Telegram generates `file_id` per-bot. A `file_id` obtained via Bot A cannot be used to download the file via Bot B — even if both bots are in the same channel. Attempting cross-bot download returns `WRONG_FILE_ID` or `400 Bad Request`.

**Why it happens:** This is documented Telegram behavior. The `file_id` encodes the bot's authorization context. Telegram's servers validate that the requesting bot matches the bot that originally received/uploaded the file.

**Consequences:**
- If the database stores `file_id` without `bot_id`, download routing breaks entirely
- If a bot token is rotated (revoked and reissued via BotFather), all existing `file_id` values for that bot become permanently invalid — files are effectively lost
- Load balancing that routes download to a different bot than the one that uploaded fails silently

**Prevention:**
- Store `bot_id` (or `worker_id`) alongside every `file_id` in the `file_chunks` table — enforce NOT NULL constraint
- Downloads must always re-use the same bot that performed the upload (look up `worker_id` from DB before requesting Telegram)
- Treat bot token rotation as a data migration event: document that rotating a token = losing all files uploaded by that token
- Add a health-check endpoint that verifies each stored bot can still access a known test file

**Detection:** `WRONG_FILE_ID` errors during download; files downloadable from one environment but not another after token change

**Phase:** Database schema design (Phase 1). The `file_chunks.worker_id` column is not optional.

---

### Pitfall 3: FILE_REFERENCE_EXPIRED Breaks Downloads Without Warning

**What goes wrong:** Telegram's MTProto API uses "file references" — short-lived byte strings embedded in the `file_reference` field of document objects. These expire after an undisclosed period. When a download is attempted with an expired reference, Telegram returns `FILE_REFERENCE_EXPIRED` RPC error. The `file_id` itself is still valid; only the reference string must be refreshed.

**Why it happens:** Telegram uses file references as a security mechanism (access control per session/context). They are not permanent identifiers. Clients are expected to refresh them by re-fetching the message that contains the file.

**Consequences:**
- Files appear to disappear from the system; users get download errors with no clear cause
- If the original message_id is not stored in the database, there is no way to refresh the reference — files are permanently inaccessible
- Projects that store only `file_id` (not `message_id`) cannot recover from this

**Prevention:**
- Store `message_id` for every uploaded chunk in the `file_chunks` table — this is the refresh anchor
- On `FILE_REFERENCE_EXPIRED`, re-fetch the message via `client.get_messages(chat_id, ids=message_id)` to get a fresh document with a new file reference, then retry the download
- Implement a transparent retry decorator for download operations that catches this specific error and refreshes automatically
- Test expiration handling explicitly — Telethon does not handle this automatically

**Detection:** `FILE_REFERENCE_EXPIRED` or `FILE_REFERENCE_INVALID` in Telethon exceptions during download

**Phase:** Download implementation phase. Must be designed in before first working download, not retrofitted.

---

### Pitfall 4: Parallel asyncio.gather Uploads Trigger FloodWaitError Prematurely

**What goes wrong:** `asyncio.gather()` fires all chunk uploads simultaneously. Each upload is a separate `sendDocument` call to Telegram. Multiple rapid calls from the same bot to the same channel trigger `FloodWaitError` (MTProto error 420), which includes a mandatory wait time in seconds. If the wait time exceeds `flood_sleep_threshold` (default: 60s), Telethon re-raises the exception instead of sleeping automatically.

**Why it happens:** Telegram's official documentation acknowledges this. From Telethon docs: "The library does not download or upload files in parallel by default because the limiting factor in the long run are FloodWaitErrors, and using parallel downloads or uploads only makes them occur sooner." The rate limit is per-bot, per-time-window — multiple bots help, but unbounded concurrency on a single bot hits limits quickly.

**Consequences:**
- Large file uploads (many chunks) fail partway through, leaving the file in `is_uploaded=False` state
- Retry storms: multiple requests re-trigger flood waits repeatedly
- If flood wait is not caught, the entire upload task crashes, orphaning partial chunk records in the database

**Prevention:**
- Use a semaphore to cap concurrent uploads per bot: `asyncio.Semaphore(3)` per bot token, not unbounded `gather`
- Set `flood_sleep_threshold` higher (e.g., `300`) on the `TelegramClient` to handle longer automatic sleeps
- Catch `FloodWaitError` explicitly; log the wait duration and retry after sleeping `error.seconds + 5`
- Distribute chunks across multiple bot tokens (the load balancer pattern already planned) — each bot has its own rate limit bucket
- Implement exponential backoff with jitter for retries, not fixed-interval retry

**Detection:** `FloodWaitError` with `seconds` attribute in Telethon exceptions; upload success rate dropping under concurrent load

**Phase:** Upload implementation phase. Semaphore must be part of the initial upload design.

---

### Pitfall 5: is_uploaded Race Condition Leaves Zombie File Records

**What goes wrong:** The upload flow sets `is_uploaded=False` when creating the file record, then sets it to `True` after all chunks are confirmed uploaded. If any chunk upload fails mid-way (crash, FloodWait, network error), the file record stays at `is_uploaded=False` indefinitely. These "zombie" records accumulate silently, consuming the file listing query and confusing users.

**Why it happens:** Without a cleanup mechanism, partial uploads have no lifecycle. If the upload task crashes after writing 5 of 10 chunk records but before completing, the database has 5 orphaned chunk rows and a file row that will never be marked `is_uploaded=True`.

**Consequences:**
- File listing shows ghost files that cannot be downloaded
- Orphaned `file_chunks` rows reference Telegram messages that do exist — but the incomplete file is unusable
- Over time, accumulation of zombie records degrades query performance on `file_chunks`

**Prevention:**
- Add a `created_at` timestamp to file records; a background job marks files as `is_uploaded=FAILED` if `is_uploaded=False` for longer than N minutes (e.g., 30 min) after `created_at`
- Implement atomic cleanup: on upload failure, delete all successfully-uploaded chunks from Telegram (using stored `message_id`) and delete the DB records in a single transaction
- Use PostgreSQL `FOR UPDATE` or application-level locking when transitioning `is_uploaded` to prevent concurrent duplicate upload attempts on the same file
- Expose a `/files/{id}/retry` endpoint for explicit re-upload of failed files

**Detection:** Growing count of `WHERE is_uploaded = FALSE AND created_at < NOW() - INTERVAL '1 hour'`; user reports of files stuck in "uploading" state

**Phase:** Upload implementation phase and background jobs phase.

---

## Moderate Pitfalls

---

### Pitfall 6: Streaming Download Buffering the Entire File in Memory

**What goes wrong:** If the download handler calls `client.download_media(message, file=bytes)` or equivalent that returns a complete bytes object, the entire file (up to 2 GB) is loaded into server RAM before the first byte is sent to the client. Under concurrent downloads, this causes OOM or severe memory pressure.

**Prevention:**
- Use `client.iter_download(document)` as an async generator and pipe it into FastAPI's `StreamingResponse`
- Never call download methods that return a complete bytes object in a request handler
- Set `request_size` on `iter_download` to a reasonable chunk (e.g., 524288 bytes = 512 KB) matching MTProto's preferred transfer unit
- Telethon's `iter_download` is confirmed memory-efficient — it yields chunks without buffering the full file

**Detection:** Memory usage spikes proportional to file size during downloads; OOM kills under concurrent download load

**Phase:** Download streaming implementation phase.

---

### Pitfall 7: MTProto Session File Conflicts When Scaling Horizontally

**What goes wrong:** Docker Compose with `scale: 3` on the API service creates 3 processes, all mounting the same volume path for `.session` files. All three try to use `bot_abc123.session` simultaneously — SQLite locking (see Pitfall 1) hits immediately under any traffic.

**Prevention:**
- Use `StringSession`: serialize the session string to PostgreSQL on first connect, load from DB on each startup — no filesystem conflicts possible
- If staying with SQLiteSession: enforce `replicas: 1` in Docker Compose until session architecture is resolved
- Alternative: assign each API replica a dedicated bot token + session so they never share sessions

**Detection:** Frequent `OperationalError: database is locked` specifically after scaling to multiple replicas

**Phase:** Infrastructure / deployment phase.

---

### Pitfall 8: PostgreSQL Connection Pool Exhaustion During Parallel Chunk Uploads

**What goes wrong:** Each chunk upload spawns an async task that may open a DB connection (to write the chunk record). With 100 MB / 20 MB = 5 chunks per file and 10 concurrent uploads = 50 simultaneous DB operations. If the connection pool size (e.g., `max_connections=10` in asyncpg) is smaller than peak demand, requests queue and timeouts cascade.

**Prevention:**
- Size the asyncpg pool to `(workers * max_concurrent_uploads * chunks_per_upload) + headroom`
- Write all chunk records for a file in a single batch insert (`INSERT INTO file_chunks VALUES ...` with multiple rows) rather than one insert per chunk — reduces connection demand by chunk count factor
- Use a single DB transaction per file upload: acquire one connection, insert file record + all chunk records atomically, release

**Detection:** `asyncpg.exceptions.TooManyConnectionsError`; increasing request latency under upload load; connection wait time visible in `pg_stat_activity`

**Phase:** Upload implementation phase (schema and DB access patterns).

---

### Pitfall 9: TelegramClient RAM Growth in Long-Running Processes

**What goes wrong:** Telethon's `TelegramClient` accumulates internal state (pending requests, update handlers, cached objects) that grows over hours/days in long-running server processes. Multiple confirmed GitHub issues (issues #3235, #4213 in LonamiWebs/Telethon) document unbounded RAM growth.

**Prevention:**
- Do not attach event handlers (`client.on(...)`) on bot-mode clients used only for file operations — update handlers are a major source of memory accumulation
- Periodically reconnect long-lived clients (e.g., every 24 hours) if memory growth is observed
- Monitor per-process RSS; add alerts at 80% of container memory limit
- Use `connection_retries=-1` to allow automatic reconnection without crashing

**Detection:** Steady RSS growth over time with stable request rate; OOM kills after days of uptime

**Phase:** Observability / production hardening phase.

---

### Pitfall 10: Bot Token as User-Provided Input — No Validation Before Storage

**What goes wrong:** User submits any string as `bot_token`. The system stores it, creates a session, and fails silently or with cryptic Telethon errors on first use. Invalid tokens, revoked tokens, or tokens belonging to bots not added to the target channel all produce different errors at different times.

**Prevention:**
- Validate the token immediately on submission: call `client.get_me()` with the token and verify the response
- Verify the bot has admin rights in the specified `chat_id` by calling `client.get_participants()` or checking permissions
- Return a user-friendly error at registration time, not during the first upload attempt
- Store bot validation status (`is_active: bool`) and surface it in the UI

**Detection:** User-reported upload failures; `AuthKeyError` or `UserNotParticipantError` from Telethon at upload time

**Phase:** Bot registration / onboarding phase.

---

## Minor Pitfalls

---

### Pitfall 11: Chunk Position Ordering Not Enforced at Write Time

**What goes wrong:** Chunks are uploaded in parallel and inserted to the database as they complete. Network variance means chunk 3 may finish before chunk 1. If the download query uses `ORDER BY created_at` instead of `ORDER BY position`, the file reassembly order is non-deterministic and the output is silently corrupted (no checksum error, just wrong bytes).

**Prevention:**
- The `file_chunks` table must have a `position` column (integer, NOT NULL)
- All download queries must use `ORDER BY position ASC` — enforce this with a query-level assertion or a code review checklist item
- Optionally store an MD5/SHA256 of the complete file at upload time for post-reassembly integrity verification

**Detection:** Downloaded files with wrong content; binary files that fail to open

**Phase:** Database schema (Phase 1) — the column must exist from day one.

---

### Pitfall 12: Telegram Premium Speed Throttling for Non-Premium Bots

**What goes wrong:** Telegram throttles upload and download speed for non-Premium accounts after transferring "tens of gigabytes or more" in a session. MTProto returns `FLOOD_PREMIUM_WAIT_X` error. This is a time-based penalty, not a rate limit that backs off immediately.

**Prevention:**
- Document this as a known limitation — encourage users to use fresh bot tokens if they hit throttling
- Implement monitoring of transfer volume per bot token; alert when approaching suspected thresholds
- Design the multi-bot architecture so that throttling one bot does not block all uploads — the load balancer should skip throttled bots

**Detection:** `FLOOD_PREMIUM_WAIT_X` errors; download/upload speed degradation without `FloodWaitError`

**Phase:** Production monitoring phase.

---

### Pitfall 13: Telethon v1 vs v2 API Incompatibility

**What goes wrong:** Telethon 2.0 (alpha, released Oct 2025) has a completely rewritten API — `TelegramClient` is replaced by `Client`, session handling changed, and many method signatures are different. Code written for v1 will not run on v2 without significant changes.

**Prevention:**
- Pin `telethon==1.x` (latest stable: 1.42.0 as of Nov 2025) in `pyproject.toml` with exact version
- Do not use `telethon>=2` or unpinned version
- Schedule a migration assessment only after v2 reaches stable release

**Detection:** `ImportError` or `AttributeError` after `pip install --upgrade telethon`

**Phase:** Project setup (pin immediately).

---

## Phase-Specific Warnings

| Phase Topic | Likely Pitfall | Mitigation |
|---|---|---|
| DB schema design | Missing `position`, `worker_id`, `message_id` columns | Make all three NOT NULL from day one (Pitfalls 2, 3, 11) |
| Session management setup | SQLite locking under concurrent access | One client per bot, unique session names, consider StringSession (Pitfall 1) |
| Parallel chunk upload | FloodWaitError crashing mid-upload | Semaphore per bot + explicit FloodWaitError catch + backoff (Pitfall 4) |
| Upload transaction logic | Zombie records from partial failures | Atomic cleanup on failure + background janitor job (Pitfall 5) |
| Download streaming | Full file buffered in RAM | `iter_download` → `StreamingResponse` generator, never bytes return (Pitfall 6) |
| Download retry logic | FILE_REFERENCE_EXPIRED silent failure | Intercept error, re-fetch message by `message_id`, retry (Pitfall 3) |
| Bot registration flow | Invalid/unauthorized bot stored | Validate with `get_me()` + channel permission check at registration (Pitfall 10) |
| Docker Compose scaling | Session file conflicts across replicas | StringSession in DB or single-replica constraint (Pitfall 7) |
| DB connection tuning | Pool exhaustion under parallel uploads | Batch inserts + pool sizing formula (Pitfall 8) |
| Production hardening | RAM growth in long-running Telethon clients | No event handlers on upload bots; periodic reconnect; RSS monitoring (Pitfall 9) |

---

## Sources

- Telethon official docs — Session Files: https://docs.telethon.dev/en/stable/concepts/sessions.html
- Telethon GitHub issue #637 — SQLite database is locked: https://github.com/LonamiWebs/Telethon/issues/637
- Telethon GitHub issue #3235 — RAM consumption increases without limit: https://github.com/LonamiWebs/Telethon/issues/3235
- Telethon GitHub issue #4213 — Memory leak wrong session ID: https://github.com/LonamiWebs/Telethon/issues/4213
- Telethon FAQ — parallel downloads/uploads and FloodWaitError: https://docs.telethon.dev/en/stable/quick-references/faq.html
- Telethon TelegramClient — flood_sleep_threshold parameter: https://docs.telethon.dev/en/stable/modules/client.html
- Telegram official API — Uploading and Downloading Files: https://core.telegram.org/api/files
- Telegram official API — File References: https://core.telegram.org/api/file-references
- Telegram official API — Error Handling: https://core.telegram.org/api/errors
- grammY docs — File Handling (file_id bot-specificity): https://grammy.dev/guide/files
- python-telegram-bot wiki — Avoiding flood limits: https://github.com/python-telegram-bot/python-telegram-bot/wiki/Avoiding-flood-limits
- Pentaract (reference implementation, Rust): https://github.com/Dominux/Pentaract
- Telethon GitHub issue #1148 — Session file reuse across connections: https://github.com/LonamiWebs/Telethon/issues/1148
