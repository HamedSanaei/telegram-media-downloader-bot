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
  size ceilings, and operator-owned semantic yt-dlp selectors.
- `yt_dlp`: cookies, an explicit yt-dlp-only proxy switch, timeouts/retries, safe filename/media
  settings, audio conversion, user agent, and the selected JavaScript runtime. These are operator
  settings, never user input.
- `security`: static allow/block sets, Redis-backed per-user request ceiling, and public-network URL
  enforcement.
- `persistence`: contained SQLite filename, selection lifetime, and cleanup interval.
- `observability`: internal health bind address/port, Telegram readiness, and metrics switch.

`media.enabled_modes` must contain `best`; callbacks use these semantic values and never raw upstream
format IDs. Storage child paths and the SQLite filename cannot escape configured roots. When
`telegram.local_api_is_local` is true, an absolute HTTP(S) `local_api_base_url` is required.
`telegram.upload_timeout_seconds` applies separately to each file/volume and defaults to 14400
seconds. `telegram.upload_chunk_size_kb` defaults to 1024 KiB and
`telegram.upload_heartbeat_interval_seconds` to 30 seconds. Text and polling retain normal bounded
timeouts. Public Bot API is capped at 50 MB; migrated Local API may use an operator ceiling up to
1900 MB.

Inspection runs each semantic selector against actual formats. Fixed modes require their exact
height and never silently fall back. `best` remains bounded to 1080p; `best_original` is never
transcoded. Displayed size is the selected video+audio sum: exact `filesize`, then estimated
`filesize_approx` or bitrate multiplied by duration. Missing data is shown as unknown instead of
hiding the option; MP3 uses configured output bitrate and duration. Final and cumulative transfer
limits remain authoritative.

Upload percentages cover bytes streamed from the tracked file into Local Bot API. Once all bytes
are read, only an elapsed-time heartbeat is shown while Telegram processes the request; Bot API
exposes no trustworthy percentage for that phase.

Cookies should be mounted read-only. A missing cookie file is simply not passed to yt-dlp; `doctor`
and operator startup review should confirm whether authenticated sources need it. Proxy credentials,
tokens, cookies, and authorization values are redacted from structured logs.

`telegram.required_channels.enabled` requires a non-empty unique channel list. Every entry has an
integer `chat_id`, display `title`, and HTTPS `t.me` join URL. The bot must be administrator in each
channel. All channels are required; positive/negative Redis cache lifetimes are separately
configurable and admins bypass membership only.

`yt_dlp.proxy_enabled` controls only source inspection/download requests. Supported schemes are
HTTP, HTTPS, SOCKS4, SOCKS4a, SOCKS5, and SOCKS5h. A legacy configuration that has `proxy` but omits
`proxy_enabled` remains enabled; explicit `false` always disables it. The value is a secret and is
never printed.

Ordinary video options carry `mp4` or `webm`. Native candidates are preferred; guaranteed fallback
uses H.264/AAC for MP4 and VP9/Opus for WebM. `best_original` remains native-only. Instagram policy
is configured under `media.instagram`: best MP4 is automatic, image entries are ignored, and the
ordered video count/aggregate size are bounded. Authenticated content uses the optional local
read-only cookies file.

After model changes regenerate and review the schema:

```bash
uv run python scripts/export_config_schema.py
```

The complete Local Bot API configuration, conditional credential rules, Windows/Linux paths, and
migration state machine are documented in `docs/LOCAL_BOT_API.md`.

## High-quality and multipart delivery

`media.enabled_modes` may include `best_original`, `video_2160`, and `video_1440`; the compatible
`best` mode remains capped at 1080p. Set both media limits to 4096 MB only when Local Bot API and
multipart routing are operational.

`multipart` owns the `7zz` path, 1850 MB volume size, 4096 MB aggregate ceiling, and store-only
compression. Every result above `telegram.max_upload_size_mb` uses this route; no Premium account,
user session, phone number, staging channel, or MTProto process is used. Setup and extraction are
documented in `docs/MULTIPART_DELIVERY.md`.
