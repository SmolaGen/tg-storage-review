# Feature Landscape

**Domain:** File Storage API (Telegram-backed, self-hosted)
**Researched:** 2026-04-06
**References:** Pentaract (Rust, 1.7k stars), TGstorage (FastAPI), Teldrive (Go), S3/GCS API conventions

---

## Table Stakes

Features users expect from any file storage API. Missing = product feels broken or incomplete.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Upload file (single request, multipart) | Baseline API capability | Low | `POST /files` with `multipart/form-data` |
| Download file by ID | Baseline API capability | Low | `GET /files/{id}/content` |
| List files with metadata | Browse what was uploaded | Low | pagination required from day 1 |
| Delete file | Basic lifecycle management | Low | Must also remove Telegram chunks from DB |
| File metadata (name, size, mime, created_at) | Users need to know what they have | Low | stored in PostgreSQL, not Telegram |
| Authentication via Bearer token (JWT) | Any API has auth | Low | per PROJECT.md: 15 min access + 30 day refresh |
| `is_uploaded` flag / pending state | Without it: partially-uploaded files appear valid | Low | Pentaract uses this pattern; critical for chunked uploads |
| Chunked upload internals (20 MB chunks) | Telegram hard limit per sendDocument | Medium | internal detail, not necessarily user-facing API |
| Correct MIME type passthrough | Download must serve correct Content-Type | Low | store on upload, return on download |
| Error responses with meaningful HTTP status codes | Industry standard | Low | 404, 413, 422, 429, 500 |

## Differentiators

Features specific to the Telegram-backed model that provide competitive advantage over S3 / generic storage.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Telegram Login Widget auth | Zero friction for Telegram users; no email/password | Low | per PROJECT.md; already decided |
| Multi-bot load balancing for uploads | Bypass 20 req/30s per-bot rate limit; parallel chunked upload | Medium | `asyncio.gather` across bots; each bot holds its own `file_id` |
| Parallel chunk upload (asyncio.gather) | Dramatically faster upload for large files vs sequential | Medium | depends on multi-bot; chunk N uploaded by bot N |
| Bot-per-chunk download (same bot that uploaded) | Telegram `file_id` is bot-specific; required for correctness | Medium | `worker_id` column in `file_chunks` table |
| StreamingResponse download | No server-side buffering; low memory usage; instant first byte | Medium | FastAPI `StreamingResponse`; chunk-by-chunk yield from Telethon |
| MTProto protocol (Telethon) vs HTTP Bot API | 2 GB file limit vs 50 MB; faster transfers via binary protocol | Medium | Telethon `bot_token` mode handles this transparently |
| Upload progress callback | Users uploading multi-GB files need feedback | Medium | Telethon supports `progress_callback`; expose via SSE or polling endpoint |
| Telegram bot interface (direct file forward) | Users can send files directly in Telegram without API client | Medium | bot receives file, calls same upload logic internally |
| Zero storage cost | Core differentiator vs S3: no per-GB fees | None (architectural) | marketing message, not a feature to build |
| Per-user isolated storage (own bot + channel) | No shared rate limits; data isolation by design | Low | already decided; enforced at data model level |

## Anti-Features

Things to deliberately NOT build in v1. Each has a reason and a "what to do instead."

| Anti-Feature | Why Avoid | What to Do Instead |
|--------------|-----------|-------------------|
| S3-compatible API | Complex compatibility layer, testing burden, not the core value prop | Own REST API is simpler, faster to ship; PROJECT.md already excluded this |
| Public/anonymous file access (no token) | Security surface, abuse potential, complicates auth model | Private-by-default; share via token in URL if needed later |
| Resumable upload protocol (tus/GCS-style) | Adds session state management, complex client SDK requirement | Server-side retries per chunk with Telegram `retry_after`; client retries whole file on failure |
| File versioning | High complexity, doubles storage consumption in Telegram channels | No versioning; overwrite = new upload, old entry deleted |
| Folder/directory hierarchy | Nice to have but adds DB complexity, query complexity | Flat file list with optional `prefix`/tag filtering is sufficient for v1 |
| Encryption at rest | Telegram already encrypts server-side; double-encrypting complicates `file_id` reuse | Document that Telegram provides server-side encryption |
| Deduplication by content hash | Requires SHA-256 of full file before upload; breaks streaming upload; limited value if per-user storage | Skip for v1; each upload is independent |
| Webhook callbacks on upload completion | Adds webhook delivery infrastructure (retries, queues) | Client polls `GET /files/{id}` status, or use SSE for progress |
| Web UI / dashboard | Out of scope for API-first service | REST API + Telegram bot is the interface |
| OAuth / email auth | Only one user group: Telegram users | Telegram Login Widget only |
| Thumbnail generation / transcoding | Media processing pipeline is a separate product | Return raw file as-is |

---

## Feature Dependencies

```
Telegram Login Widget
  → JWT issuance
    → All authenticated endpoints

Bot token registration (user provides bot_token + chat_id)
  → Multi-bot pool
    → Parallel chunk upload
      → is_uploaded flag + file_chunks table
        → Download (reassemble from file_chunks, bot-specific file_id)
          → StreamingResponse download
          → Delete (remove chunk records)

Multi-bot pool
  → Rate limit distribution (bot-level: 20 uploads/30s per bot)

Telegram bot interface
  → Same upload pipeline (reuses chunk logic)
  → Requires JWT or session tie to user account
```

---

## MVP Recommendation

**Prioritize (ship together — they are a single vertical slice):**

1. Telegram Login Widget + JWT auth
2. Bot token + chat_id registration
3. `POST /files` upload — chunked internally, `is_uploaded` flag, parallel via asyncio.gather
4. `GET /files/{id}/content` — StreamingResponse, bot-specific file_id lookup
5. `GET /files` — list with metadata (name, size, mime, created_at, is_uploaded)
6. `DELETE /files/{id}` — remove DB records (Telegram doesn't support deleting messages via bot)

**Second pass (high value, low risk):**

7. Upload progress via `GET /files/{id}/status` polling (bytes_uploaded / total_bytes)
8. Telegram bot interface for direct file upload (forward to bot)
9. Multi-bot balancing (add/remove bots via `POST /bots`)

**Defer (post-MVP):**

- Rename file: simple DB update, low complexity but low urgency
- Search by filename prefix: basic SQL LIKE, add when file list grows
- Per-file expiry / TTL: useful for CDN use cases but not core
- Share links with token: `GET /files/{id}/share` returns time-limited URL

---

## Telegram-Specific Constraints That Shape Features

| Constraint | Impact on Features |
|------------|-------------------|
| `file_id` is bot-specific | Chunk must be downloaded by the same bot that uploaded it; `worker_id` stored per chunk |
| 20 sendDocument calls per 30 seconds per bot | Multi-bot pool is not a differentiator — it is a correctness requirement for large files |
| `retry_after` returned on rate limit (Bot API 7.0+) | Upload pipeline must handle 429 with backoff; not optional |
| MTProto 2 GB file cap | Documented hard limit; client must split files >2 GB before sending (out of scope for v1) |
| Telegram does not support message deletion via bot in channels where bot is not admin | Delete from DB only; Telegram channel retains the raw chunks (orphaned data) |
| Telegram CDN redirect on download | Telethon handles automatically; transparent to application layer |

---

## Confidence Assessment

| Area | Confidence | Source |
|------|------------|--------|
| Table stakes (upload/download/list/delete) | HIGH | Industry standard; Pentaract, TGstorage, S3 docs |
| Chunked upload pattern, file_id/worker_id | HIGH | Pentaract reference architecture; Telethon docs |
| Rate limits (20/30s per bot) | MEDIUM | Multiple sources agree; exact limits may vary per bot reputation |
| MTProto 2 GB limit | HIGH | PROJECT.md + Telethon docs |
| Resumable upload complexity (why to avoid) | MEDIUM | GCS/tus protocol analysis; confirmed by Pentaract issues |
| Deduplication complexity/value | MEDIUM | Pattern analysis; no single authoritative source |

---

## Sources

- [Pentaract GitHub (Dominux/Pentaract)](https://github.com/Dominux/Pentaract) — reference architecture, issues
- [TGstorage GitHub (DraxonV1/TGstorage)](https://github.com/DraxonV1/TGstorage) — FastAPI implementation patterns
- [Teldrive GitHub (tgdrive/teldrive)](https://github.com/tgdrive/teldrive) — Go implementation, rclone integration
- [REST File Uploads Best Practices — OneUptime 2026](https://oneuptime.com/blog/post/2026-02-02-rest-file-uploads/view)
- [Telethon 1.42.0 Documentation](https://docs.telethon.dev/en/stable/modules/client.html) — progress_callback, upload/download API
- [Telegram Bot Rate Limits — BytePlus](https://www.byteplus.com/en/topic/450604)
- [25 File Storage APIs — Unified.to 2026](https://unified.to/blog/25_file_storage_apis_to_integrate_with_in_2026_google_drive_dropbox_s3_and_unified_storage_apis)
