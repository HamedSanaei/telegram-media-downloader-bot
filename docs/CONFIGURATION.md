# Configuration reference

The application reads one local YAML file. The default is `config.yaml`; `APP_CONFIG_PATH` may select
another file, but secrets themselves remain in YAML. Start from `config.example.yaml`. Unknown keys
fail startup and `config.yaml` is ignored by Git and Docker build context.

## Sections

- `app`: environment, structured/console logging, language, and timezone.
- `telegram`: token/admins, polling, automatic or document-only delivery, upload ceiling, sanitized
  caption template (`{title}` and `{source}` only), filename length, progress throttling, and optional
  local Bot API base URL.
- `redis`: ARQ/rate-limit Redis DSN and queue name.
- `queue`: concurrency, timeout, attempts, retry delay, and ARQ result retention.
- `storage`: contained download/temp/state paths, terminal cleanup, orphan grace, and job retention.
- `media`: source allowlist, enabled semantic modes, default mode, playlist policy, duration/size
  ceilings, and operator-owned semantic yt-dlp selectors.
- `yt_dlp`: cookies/proxy/timeouts/retries, safe filename/media settings, audio conversion, user agent,
  and the selected JavaScript runtime. These are operator settings, never user input.
- `security`: static allow/block sets, Redis-backed per-user request ceiling, and public-network URL
  enforcement.
- `persistence`: contained SQLite filename, selection lifetime, and cleanup interval.
- `observability`: internal health bind address/port, Telegram readiness, and metrics switch.

`media.enabled_modes` must contain `best`; callbacks use these semantic values and never raw upstream
format IDs. Storage child paths and the SQLite filename cannot escape configured roots. When
`telegram.local_api_is_local` is true, an absolute HTTP(S) `local_api_base_url` is required.

The size shown during inspection is advisory upstream metadata and may refer to the source's default
or best format rather than the user's later semantic selection. `media.max_file_size_mb` is enforced
by selecting the highest-quality complete configured stream combination whose known aggregate size
fits. The exact final post-processed file is checked again before Telegram delivery. Consequently,
buttons labeled “up to 1080p/720p/480p” may select a lower resolution when the requested ceiling
cannot fit without dropping audio or exceeding the configured size. When upstream size metadata is
missing, cumulative downloaded bytes are bounded during transfer and the final file is still checked.

Cookies should be mounted read-only. A missing cookie file is simply not passed to yt-dlp; `doctor`
and operator startup review should confirm whether authenticated sources need it. Proxy credentials,
tokens, cookies, and authorization values are redacted from structured logs.

After model changes regenerate and review the schema:

```bash
uv run python scripts/export_config_schema.py
```
