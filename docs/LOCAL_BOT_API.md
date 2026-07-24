# Telegram Local Bot API

The Local Bot API is optional and is not a Userbot. It uses only the bot token plus Telegram
application `api_id`/`api_hash`. It never uses a phone number, login code, two-step password, user
session, or MTProto user account.

Telegram's [Local Bot API documentation](https://core.telegram.org/bots/api#using-a-local-bot-api-server)
documents a 2000 MB upload ceiling. This project caps the configurable production value at 1900 MB
to leave operational headroom. The public Bot API runtime is capped at 50 MB even if a future local
configuration contains a larger value.

## Obtain credentials

1. Create or select an application at <https://my.telegram.org/apps>.
2. Copy its numeric `api_id` and `api_hash`.
3. Store those values only in ignored local `config.yaml`.
4. Restrict the file to the service account (`chmod 600 config.yaml` on Linux and an equivalent
   user-only ACL on Windows).

`api_id` and `api_hash` are required only for `enabled: true` plus `mode: managed`. An external
server owns its own credentials; this application receives only its base URL.

## Configuration

The safe committed example remains disabled and contains placeholders. A managed local profile is:

```yaml
telegram:
  bot_token: "REPLACE_IN_LOCAL_CONFIG"
  max_upload_size_mb: 1900
  local_api_base_url: "http://127.0.0.1:8081"
  local_api_is_local: true
  local_bot_api:
    enabled: true
    mode: managed
    executable: "C:/Tools/telegram-bot-api/telegram-bot-api.exe"
    api_id: 123456
    api_hash: "REPLACE_IN_LOCAL_CONFIG"
    host: "127.0.0.1"
    port: 8081
    local_mode: true
    working_directory: "./data/telegram-bot-api"
    temp_directory: "./data/telegram-bot-api/temp"
    log_file: "./data/telegram-bot-api/telegram-bot-api.log"
    verbosity: 2
    auto_start: true
    startup_timeout_seconds: 30
    shutdown_timeout_seconds: 20
    migration:
      auto_logout_from_cloud: false
      state_file: "./data/state/telegram-api-migration.json"

media:
  max_file_size_mb: 1900
  max_source_size_mb: 2000
```

Relative Local API paths resolve from the directory containing `config.yaml`, using `pathlib` on
Windows and Linux. `api_hash` and `api_id` are injected into the managed child process from values
already parsed from YAML; neither is placed on its command line. No application setting or secret
is sourced from the parent environment.

For an external server set `mode: external`, keep `enabled: true`, set the base URL, and omit
`executable`, `api_id`, and `api_hash`. Lifecycle commands do not start or stop external processes.

## Windows setup

Build or obtain the [official `telegram-bot-api` binary](https://github.com/tdlib/telegram-bot-api),
place it in a directory readable and executable by the service account, and use forward slashes or
a quoted YAML path. Ensure the configured port is available:

```powershell
uv run telegram-media-bot config-check --config .\config.yaml
uv run telegram-media-bot doctor --config .\config.yaml
uv run telegram-media-bot local-api --config .\config.yaml status
uv run telegram-media-bot local-api --config .\config.yaml start
```

Give only that Windows account access to `config.yaml`, the server work directory, state directory,
and log file. Do not run the bot as Administrator.

## Linux setup

Build the official binary, install it outside the repository (for example
`/opt/telegram-bot-api/bin/telegram-bot-api`), and grant the unprivileged service account execute
permission. Keep writable state below a dedicated directory:

```bash
chmod 600 config.yaml
uv run telegram-media-bot config-check --config ./config.yaml
uv run telegram-media-bot doctor --config ./config.yaml
uv run telegram-media-bot local-api --config ./config.yaml start
```

Bind to loopback unless a protected container network requires another address. Do not publish an
unencrypted Local Bot API port to an untrusted network.

## Explicit migration to local

Stop both normal application processes before migration. Endpoint leases make the command fail
closed if a live Bot or Worker is detected.

```bash
uv run telegram-media-bot local-api --config ./config.yaml status
uv run telegram-media-bot local-api --config ./config.yaml migrate-to-local
```

The command requires typing `MIGRATE-TO-LOCAL` (or an explicit operator-provided `--yes`). It starts
or checks the Local API first, atomically records `cloud_logout_pending`, calls `logOut` exactly once
against the public Bot API, verifies `getMe` against the local endpoint, and records `local`.
Ordinary Bot and Worker startup never call `logOut`.

If the cloud request outcome is uncertain, state becomes `cloud_logout_uncertain`; rerunning the
command does not repeat the request. Preserve the state file and investigate connectivity before an
operator resolves the incident. Do not delete or edit migration state casually.

After successful migration, start Worker and Bot. Both use the same config-derived endpoint and
register process leases; a cloud/local mixture is rejected.

## Rollback to cloud

Stop Bot and Worker, then run:

```bash
uv run telegram-media-bot local-api --config ./config.yaml migrate-to-cloud
```

Confirm with `MIGRATE-TO-CLOUD`. The command calls `logOut` once against the local server, records a
10-minute `cloud_wait` required by Telegram, and stops a managed server. Normal startup remains
blocked until the recorded time passes. Then:

```bash
uv run telegram-media-bot local-api --config ./config.yaml status
```

Once `active_endpoint` is `cloud`, disable `local_bot_api`, restore the cloud-safe upload/media
limits, run `config-check`, and restart the application. Never run polling simultaneously against
both endpoints.

## Docker Compose

The base Compose file contains no Local API secrets or endpoint environment variables. It mounts
only the ignored YAML and passes its path explicitly. Two supported optional deployments are:

- `external`: run the official server on a protected adjacent host/network and configure its URL in
  YAML;
- `managed`: mount a compatible official executable into the application image and use absolute
  writable paths below `/data` in YAML.

The project image does not bundle the official server. Do not place `api_id`, `api_hash`, or the bot
token in Compose, Dockerfile, build arguments, image layers, or `.env`.

## Health, limits, and delivery

`config-check` validates conditional credentials, executable, URL/port, writable directories, and
migration state. `doctor` additionally requires the configured endpoint to be reachable. Output is
boolean/status-only and never prints credentials or Local API paths. Worker readiness includes a
`local_bot_api` component: it must be reachable while local is the active endpoint.

The download adapter transcodes only when the produced file is larger than
`telegram.max_upload_size_mb`. Files below that ceiling are delivered unchanged. The independent
`media.max_file_size_mb` remains the operator's content-policy ceiling and should normally match the
local upload ceiling. Telegram delivery performs a final exact byte check.

## Large-file verification

Unit tests cover a declared 201 MB delivery without allocating that payload. A real sparse 201 MB
upload is deliberately destructive and opt-in; it uses the first configured `telegram.admin_ids`
entry and deletes the test message afterward:

```bash
RUN_LOCAL_API_LARGE_FILE_TEST=1 \
  uv run pytest -m large_file tests/integration/test_local_api_large_upload.py
```

On PowerShell set the same one-shot test switch for the process. The bot must already be migrated to
local. Never run this test in the default CI suite.

## Troubleshooting

- `active_endpoint: blocked`: complete or investigate the explicit migration; normal startup is
  intentionally fail-closed.
- `endpoint_reachable: false`: verify host/port, firewall, server log permissions, and that another
  process is not using the port.
- `Stop the bot and worker...`: stop both application processes, then retry migration.
- `cloud_wait`: wait until Telegram's recorded 10-minute cloud reuse interval expires.
- `credentials` or `executable` fails in `doctor`: correct the ignored YAML or filesystem
  permissions. Secrets are intentionally never echoed.
- Upload rejected near 1900 MB: compare exact output bytes, free disk, request timeout, reverse
  proxies, and the configured Telegram/media ceilings.
