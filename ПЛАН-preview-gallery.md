# ПЛАН: Preview Gallery (фото/видео)

## Context
Добавить галерею превью в веб-интерфейс. `/thumbnail` endpoint уже есть (WebP, ETag, 120 req/min),
но в HTML нет ни одного `<img>` тега и нет disk cache — каждый запрос скачивает весь файл из Telegram.
Видео: animated WebP (первые 5 сек, hover) через ffmpeg. Placeholder: blurhash из БД.

## Approach
1. **Disk cache** в thumbnail service — hash-based путь `/tmp/tg-thumbnails/{h[:2]}/{h[2:4]}/{h}.webp`; cache hit = 0 Telegram запросов
2. **Preset sizes** {200, 400, 800} — ограничить query param; кэш растёт линейно, не бесконечно
3. **Blurhash** в модели File — вычислять при генерации thumbnail, хранить в БД, возвращать в `GET /files`
4. **Pre-generate на upload** — в `_background_upload` после `ingest_file` генерировать thumbnail 200px (image/* + video/*)
5. **Video poster** через ffmpeg — статичный первый кадр (ss=1s) для `<video poster>`
6. **Video animated preview** — новый endpoint `GET /files/{id}/preview`: animated WebP, 5 сек, 3 fps, кэш обязателен
7. **Frontend gallery** — grid layout, `<img loading="lazy">` + IntersectionObserver + blurhash canvas placeholder; видео — hover переключает `<img>` на animated preview
8. **Cache eviction** при soft delete — удалять disk cache для всех пресетов

## Files
- `app/services/thumbnail.py` — NEW: disk cache + blurhash + video poster (ffmpeg ss=1) + animated preview (ffmpeg -t 5 fps=3)
- `app/api/files.py` — preset sizes validation; новый endpoint `GET /{file_id}/preview`; интеграция thumbnail service
- `app/services/file.py` — `_background_upload`: pre-generate; `soft_delete_file`: cache eviction
- `app/models/file.py` — поле `blurhash: Mapped[str | None]`
- `app/schemas/files.py` — `blurhash: str | None` в FileStatusResponse и FileListItem
- `app/static/index.html` — gallery grid + lazy loading + blurhash canvas + animated hover для видео
- `alembic/versions/` — NEW: ADD COLUMN blurhash TEXT
- `requirements.txt` — blurhash-python==1.1.3, ffmpeg-python==0.2.0

## Reuse
- `app/services/file.py:download_file_bytes` (~line 385) — скачать файл в память для thumbnail gen
- `app/api/files.py:/files/{file_id}/thumbnail` — существующий endpoint с ETag + Cache-Control headers
- Нативные `loading="lazy"` и `IntersectionObserver` — без доп. библиотек

## Verification
```bash
# cache miss → ~500ms (Telegram download)
time curl -o /dev/null http://localhost:8000/files/{id}/thumbnail?size=200
# cache hit → ~5ms (disk read)
time curl -o /dev/null http://localhost:8000/files/{id}/thumbnail?size=200
# animated preview видео
curl -o preview.webp http://localhost:8000/files/{id}/preview
```

## Estimation
~8 файлов, размер L (1.5-2 дня)
