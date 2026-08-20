# Configuration reference

The application reads one local YAML file. The default is `config.yaml`; `APP_CONFIG_PATH` may select
another file, but secrets themselves remain in YAML. Start from `config.example.yaml`. Unknown keys
fail startup and `config.yaml` is ignored by Git and Docker build context.

## Sections

- `app`: environment, structured/console logging, language, and timezone.
- `telegram`: token/admins, polling, automatic or document-only delivery, upload ceiling and upload
  request timeout, upload chunk/heartbeat intervals, sanitized caption template (`{title}`,
  `{source}`, and `{bot_username}`), required channels, filename length, progress throttling, Local
  Bot API endpoint, managed/external lifecycle, and migration state.
- `redis`: ARQ/rate-limit Redis DSN and queue name.
- `queue`: concurrency, timeout, attempts, retry delay, and ARQ result retention.
- `storage`: contained download/temp/state paths, terminal cleanup, orphan grace, and job retention.
- `media`: source allowlist, enabled semantic modes, default mode, playlist policy, final/source
  size ceilings, operator-owned semantic yt-dlp selectors, Instagram policy, bounded FFmpeg
  transcoding controls, and terminal workspace cleanup switches.
- `yt_dlp`: cookies, an explicit yt-dlp-only proxy switch, timeouts/retries, safe filename/media
  settings, audio conversion, user agent, and the selected JavaScript runtime. These are operator
  settings, never user input.
- `security`: static allow/block sets, Redis-backed per-user request ceiling, and public-network URL
  enforcement.
- `persistence`: contained SQLite filename, selection lifetime, and cleanup interval.
- `observability`: internal health bind address/port, Telegram readiness, and metrics switch.
- `operations`: safe release-maintenance controls, including project-only old-image cleanup.

`media.enabled_modes` must contain `best`; callbacks use these semantic values and never raw upstream
format IDs. Storage child paths and the SQLite filename cannot escape configured roots. When
`telegram.local_api_is_local` is true, an absolute HTTP(S) `local_api_base_url` is required.
`telegram.upload_timeout_seconds` applies separately to each file/volume and defaults to 14400
seconds. `telegram.upload_chunk_size_kb` defaults to 1024 KiB and
`telegram.upload_heartbeat_interval_seconds` to 30 seconds. Text and polling retain normal bounded
timeouts. Public Bot API is capped at 50 MB; migrated Local API may use an operator ceiling up to
1900 MB.

Inspection runs each semantic selector against actual formats. Fixed modes normally require their
exact height. MP4 Native follows `media.mp4_native_fallback`: `lower_resolution` (default) selects
the highest lower native H.264/AAC plan; duplicate requested modes are collapsed and only its actual
height is displayed. `fail` offers no lower choice. `best_original` is never transcoded. Displayed
size is the selected video+audio sum: exact `filesize`, then estimated
`filesize_approx` or bitrate multiplied by duration. Missing data is shown as unknown instead of
hiding the option; MP3 uses configured output bitrate and duration. Final and cumulative transfer
limits remain authoritative.

No configuration switch is required for YouTube single-video intent. A valid video ID in `watch`,
`youtu.be`, `shorts`, or `live` form takes precedence over Mix/playlist query parameters; those
links are canonicalized and passed to yt-dlp with `noplaylist`. Explicit `/playlist?list=...` URLs
still follow `media.allow_playlists` and `media.playlist_max_items`.

Upload percentages cover bytes streamed from the tracked file into Local Bot API. Once all bytes
are read, only an elapsed-time heartbeat is shown while Telegram processes the request; Bot API
exposes no trustworthy percentage for that phase.

The canonical cookie file should be restricted to the runtime owner with mode `0600` and must be
writable when administrator cookie management is enabled. A configured missing or unreadable file
fails `config-check`/`doctor`; yt-dlp no longer silently drops the configured path. Proxy
credentials, tokens, cookies, and authorization values are redacted from structured logs.

`telegram.required_channels.enabled` requires a non-empty unique channel list. Every entry has an
integer `chat_id`, display `title`, and HTTPS `t.me` join URL. The bot must be administrator in each
channel. All channels are required; positive/negative Redis cache lifetimes are separately
configurable and admins bypass membership only.

Every ID in `telegram.admin_ids` receives the persistent management reply keyboard after `/start`,
`/menu`, or `/panel`. The same list authorizes every management button and report refresh callback;
no role is inferred from a previously visible keyboard or FSM state. Administrators retain the
existing required-channel bypass. Their inspection/download activity remains durable for audit and
idempotency but is excluded from public weekly, monthly, and complete usage KPIs. Changing the list
takes effect when the bot restarts and reloads `config.yaml`.

`yt_dlp.proxy_enabled` controls only source inspection/download requests. Supported schemes are
HTTP, HTTPS, SOCKS4, SOCKS4a, SOCKS5, and SOCKS5h. A legacy configuration that has `proxy` but omits
`proxy_enabled` remains enabled; explicit `false` always disables it. The value is a secret and is
never printed.

Ordinary video options carry fast `mp4` or native `webm`. Fast MP4 admits only native MP4
H.264/AVC (`h264`/`avc1`) video plus M4A/MP4 AAC (`aac`/`mp4a`) audio before quality and bitrate
ranking. AV1/`av01` and VP9/`vp09` in MP4 are not fast-MP4 candidates and never trigger a hidden
conversion. Native WebM remains VP9 + Opus. `best_original` remains native-only. Instagram policy
is configured under `media.instagram`: automatic downloads always use `best_original` with
`NATIVE_ONLY`. When `force_mp4: true`, the best native `ext=mp4` video and `ext=m4a` audio are
selected and only merged/remuxed. When `force_mp4: false`, no container is hard-coded and the
source-selected container/codecs are preserved. Image entries are ignored, and the ordered video
count/aggregate size are bounded. Authenticated content uses the optional canonical local cookie
file.

Bulk Instagram Stories/Highlight batches are bounded per job by
`media.instagram.max_stories_per_batch` and `media.instagram.max_highlight_items`; the aggregate
collection size is bounded by the source/gallery total limits and never by the single-file
`max_file_size_mb`.

### Cookie Health Center

`cookie_health` is admin-only diagnostics over the canonical combined cookie file (the file stays
the single source of truth): `expiring_soon_hours` (default `24`) controls the static
EXPIRING_SOON threshold; `reminder_interval_minutes` (default `180`) deduplicates repeated passive
runtime-failure alerts; and
`recovery_notifications` (default `true`) sends one alert when a provider returns to HEALTHY.
All refreshes are local/static. Cookie Health has no scheduled watcher and never contacts a
provider to validate cookies; authentication failures are learned only from an existing real user
extraction. The removed `expiry_watch_interval_minutes`, `active_probe_interval_minutes`,
`probe_timeout_seconds`, `probe_concurrency`, and `probes` keys are accepted and ignored so older
configuration files continue to start. They are omitted from the example and generated schema.

`telegram.upload_as_document: true` accepts native media without requiring Telegram's inline-video
profile. In particular, VP9 inside MP4 is valid document delivery and is not converted merely
because `send_video` prefers H.264/AAC. The media size setting is a hard ceiling, not a target:
forced codec conversion starts with CRF (`libx264` for MP4), and only an oversized or
disproportionately inflated quality pass activates bitrate-limited fallback.

`media.transcode` and `media.mp4_native_fallback` defaults keep v1.0.0 through v1.0.5 files valid
without manual edits in v1.0.6:
`enabled: true`, `explicit_mp4_enabled: false`, `mp4_native_fallback: lower_resolution`,
`threads: 2`, `max_concurrent: 1`, `timeout_seconds: 1500`, and
`progress_interval_seconds: 10`. The thread value is passed to FFmpeg itself; the concurrency gate
is process-local to the supported single-worker topology. `explicit_mp4_enabled` remains an
internal conversion capability and never exposes a public Telegram button. Before FFmpeg starts in
an authorized non-public flow, a conservative estimate uses duration,
pixels, FPS, source codec, encoder threads, detected cgroup CPU capacity, and the stage timeout; an
unsafe estimate is rejected with native-lower/Best Original guidance. `TMB_WORKER_CPUS` in `.env`
optionally adds a persistent Docker CPU
quota (for example `1.5`); its default `0` leaves Docker unlimited.

After model changes regenerate and review the schema:

```bash
uv run python scripts/export_config_schema.py
```

The complete Local Bot API configuration, conditional credential rules, Windows/Linux paths, and
migration state machine are documented in `docs/LOCAL_BOT_API.md`.

## Gallery-dl images and mixed posts

`gallery_dl.enabled` defaults to `true`. The remaining owned controls bound a single subprocess:
`timeout_seconds: 600`, `max_assets_per_job: 30`, `max_total_size_mb: 1024`,
`max_concurrent_processes: 2`, and `sleep_request_seconds: 1`. `enabled_platforms` accepts only
`instagram`, `tiktok`, `twitter`, and `pinterest`. Arbitrary gallery-dl option dictionaries are not
accepted.

`yt_dlp.cookies_file` is the canonical combined Netscape file used by yt-dlp, SoundCloud, Instagram,
TikTok, Twitter/X, and Pinterest. The older keys under `gallery_dl.cookies` remain compatibility
aliases only. Leave them null, or point every non-null entry to the exact same canonical file;
divergent paths fail fast. If `yt_dlp.cookies_file` is null and exactly one effective legacy path is
configured, that path is promoted for every consumer. Files are opened afresh by each job, and
`config-check`/`doctor` validate the same effective path without reporting its contents or path.
Relative explicit paths resolve from the config directory.

Image validation defaults to 20,000 pixels per dimension and 100,000,000 total pixels. Telegram
albums are capped at 10 items per request and collections are chunked without reordering or a
one-item media group. `zip_threshold` documents the point at which the ZIP action is preferred;
the user may still choose an ordered original-image ZIP for a smaller gallery.

Existing v1.0.x YAML files remain valid because the entire section has typed defaults. The new
maximum must not exceed `multipart.max_total_size_mb`. Config/state/Redis/cookies/downloads are not
rewritten during update.

## High-quality and multipart delivery

`media.enabled_modes` may include `best_original`, `video_2160`, and `video_1440`; the compatible
`best` mode remains capped at 1080p. Set both media limits to 4096 MB only when Local Bot API and
multipart routing are operational.

`multipart` owns the preferred `7zz` path, 1850 MB volume size, 4096 MB aggregate ceiling, and
store-only compression. A bare-metal `7z` installation is accepted as a compatible fallback, and
the image guarantees both command names. Every result above `telegram.max_upload_size_mb` uses this route; no Premium account,
user session, phone number, staging channel, or MTProto process is used. Setup and extraction are
documented in `docs/MULTIPART_DELIVERY.md`.

## Zero-retention and project image cleanup

`media.workspace.cleanup_on_success`, `cleanup_on_failure`, `cleanup_on_cancel`, and
`cleanup_on_timeout` default to `true`. Together with the existing
`storage.delete_after_upload: true`, they enforce terminal cleanup of the exact job directories.
Keep these defaults enabled in production. `storage.orphan_grace_seconds` applies only to unknown,
non-terminal directories discovered by the sweeper; known terminal jobs are cleaned immediately.
Neither cleanup path follows symlinks or removes the shared workspace roots.

`operations.update.prune_old_project_images_after_success` defaults to `true`. After a verified
update, `tmb`
may remove unreferenced old images only from
`ghcr.io/hamedsanaei/telegram-media-downloader-bot`. Run `tmb cleanup --dry-run` to preview
workspace, stopped-project-container, and old-image candidates. This command never invokes
`docker system prune`, `docker image prune`, `docker volume prune`, or build-cache pruning.
