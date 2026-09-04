# tmb — Operator Control Plane Reference

`tmb` is the single authoritative management interface for the Telegram Media Downloader Bot.
It combines an interactive menu (run `tmb` with no arguments) with a scriptable command line;
every menu action is the same handler as its non-interactive command, so automation never needs
to scrape menus. All state-mutating operations take a management lock, so concurrent
`tmb update` / `tmb restore` / `tmb backup create` / cleanup operations cannot race.

- Colors are used only on interactive terminals; no functionality depends on them.
- Destructive operations require an exact typed confirmation (for example
  `Type DELETE-DOWNLOADS to continue:`) or the explicit `--yes` flag for automation.
  Consent is never silently inferred from the absence of a terminal.
- Secrets (bot tokens, API hashes, cookies, proxy credentials) are never printed. Diagnostic
  output passes through a central redaction filter.
- Interactive: `tmb` → nested menus with `0) Back`.
- Help: `tmb help`, `tmb --help`, `tmb help COMMAND` (for example `tmb help backup`),
  `tmb version`, `tmb --version`.

## Service lifecycle

```bash
tmb start                     # start all services (never resurrects intentionally stopped ones)
tmb stop                      # stop all services; persistent data and volumes are preserved
tmb restart                   # recreate all services
tmb services ps               # current compose state
tmb services health           # per-service state, health, restart count, uptime
tmb services start-one bot    # also: worker | local-api | redis
tmb services stop-one bot
tmb services restart-one bot
```

The compose `restart: unless-stopped` contract is preserved: an intentional `stop` is never
undone by later `start`/`update` operations.

## Status / dashboard

```bash
tmb status
```

Shows the installed version, configured image and digest, per-service container state/health/
restart count/uptime, a CPU/memory summary when available, root filesystem usage, project/data/
downloads/temp/state/cookies/Local API/backup sizes, the SQLite database size, the project Redis
volume size, approximate reclaimable old-image space, Telegram bot connectivity (never the
token), logger state, required-channel policy state, and the Cloud/Local Bot API migration state.

## Logs and diagnostics

```bash
tmb logs                        # last 100 lines, all services
tmb logs bot | worker | local-api | redis
tmb logs --tail 500
tmb logs --since 2h
tmb logs worker --tail 200 --since 24h
tmb logs errors                 # error lines, last 24h
tmb logs -f                     # follow live
tmb bundle                      # sanitized diagnostic support bundle
```

All log output passes through the central redaction filter. `tmb bundle` creates a 0600 archive
under `backups/` with versions, image digest, compose state, environment, disk usage, sanitized
recent logs, doctor/config-check output, SQLite integrity, Redis health, and Local API state —
never `config.yaml`, cookies, sessions, or secrets.

## Storage

```bash
tmb storage                       # directory sizes
tmb storage cleanup-downloads --yes
tmb storage cleanup-temp --yes
tmb storage orphan-workspaces --yes
tmb storage old-backups [KEEP] --yes   # default keep = 10
tmb cleanup --dry-run             # backward-compatible workspace+image cleanup
```

Every cleanup is bounded to a project-owned root. Default cleanup never removes `config.yaml`,
`.env`, the SQLite database, cookies, sessions, Local Bot API durable state, vault data, backup
files, or Redis data.

## Backup, restore, and migration

```bash
tmb backup create                # consistent operational backup (stops writers briefly)
tmb backup list
tmb backup inspect FILE
tmb backup verify FILE           # gzip, checksum, manifest, safe entries
tmb backup secure FILE           # re-secure a copied-in archive/checksum to 0600
tmb backup delete FILE [--yes]
tmb migration export             # portable bundle for a new server
tmb migration export --include-downloads   # explicit opt-in, requires confirmation
tmb migration import FILE        # transactional import (migration bundles only)
tmb restore --dry-run FILE       # validate without changing anything
tmb restore FILE                 # transactional restore with automatic rollback
```

Backups are `0600` archives with a manifest (schema version, kind, app version, image, contents)
and a sibling SHA-256 checksum (also `0600`). They contain `config.yaml`, `.env`, `data/state`,
`data/cookies`, and Local Bot API durable state; downloads/temp are excluded unless explicitly
requested. Verification never prints secrets: archive paths and manifest values pass through the
central redaction filter, so a Local Bot API directory whose name embeds a bot token is displayed
redacted while the bundle itself remains fully restorable. If an archive copied to this server
(for example via scp) is group/world readable, `tmb backup verify`/`import` print a warning and
suggest `tmb backup secure FILE`; the import itself still works.

Restore is transactional: it verifies the archive (format, checksum, path-traversal and symlink
rejection), records the exact running service state, stops filesystem writers, creates a
pre-restore safety backup, extracts and validates the staged state (config-check, SQLite
integrity), swaps persistent entries with a rollback snapshot, repairs runtime permissions
(including chowning the restored `config.yaml` to the restored `APP_UID`/`APP_GID` from the
restored `.env`, mode `0600`, so source and destination may run different runtime UID/GID),
probes runtime writes, runs an offline doctor, restores the exact previous service state, and
verifies health online. Any failure (including SIGINT) rolls back automatically, restoring the
original owner, mode, and contents; if rollback itself fails, recovery material is preserved and
exact recovery instructions are printed.

### Moving servers (migration)

A migration bundle carries `config.yaml`, `.env`, SQLite/state, cookies, and Local Bot API
durable state between two independent installations — the source and destination directories
need not match, and neither do their runtime `APP_UID`/`APP_GID` (the import re-owns the restored
private config for the destination runtime identity). Before the final destination activation you
MUST stop the source bot and worker (`tmb stop` on the old server) so the same Telegram bot is
never polling/working on both servers at the same time:

```bash
# 1) old server: export + stop
cd <install> && tmb migration export
cd <install> && tmb stop          # required BEFORE destination activation
# copy the produced backups/tmb-*.tar.gz (+ .sha256) archive to the new server

# 2) new server: bootstrap the destination, import, verify, start
bash <(curl -fsSL https://raw.githubusercontent.com/HamedSanaei/telegram-media-downloader-bot/main/install.sh) --migration
#   installs verified files, Docker, directories, the pinned image, and the tmb command;
#   runs NO token wizard, starts NO bot/worker, activates NO Local Bot API
cd <install> && tmb migration import ./backups/tmb-*.tar.gz
cd <install> && tmb status        # configuration/state restored; services still stopped
cd <install> && tmb start         # activate on the destination only after the source is stopped
```

Never run the source and destination stacks simultaneously for the same bot: Telegram polling
and durable job recovery are not cluster-safe. `tmb migration import` refuses to activate any
service by itself; the operator starts the destination explicitly, and only after the source has
been stopped. The migration bundle never contains or restores Telegram session/login state that
requires secrets outside `config.yaml`; supported persistent items are limited to what is listed
above, and everything secret stays `0600` inside the archive.

## Docker (project-scoped)

```bash
tmb docker status | version | compose-version
tmb docker containers | images | volumes
tmb docker current-image          # image reference + digest
tmb docker pull                  # pull the configured image
tmb docker pull-latest           # pull the latest release image
tmb docker recreate
tmb docker cleanup-preview       # old project images that could be reclaimed
tmb docker cleanup-old-images --yes
tmb docker compose-config        # validate compose configuration
tmb docker build                 # build a local development image
```

Cleanup targets only project images (`ghcr.io/hamedsanaei/telegram-media-downloader-bot`) that
no container references. No server-wide prune is ever used for normal project cleanup.

## Update / rollback

```bash
tmb update                       # transactional verified updater
TMB_RELEASE_TAG=v1.3.8 tmb update   # pin a specific release
tmb version
```

The updater verifies the release checksum, blocks known-broken releases, pre-downloads and
validates the candidate, stops writers, takes a consistent backup, transactionally replaces the
application, changes the image, repairs runtime permissions, runs offline and online doctors, and
restores the exact pre-update service state. Any failure — including SIGINT — rolls back the
application, image, permissions, and service state automatically. The updater runs from an
isolated copy so it can replace its own installed files safely.

## Telegram setup

```bash
tmb telegram status              # token configured, mode, bot username when verified
tmb telegram setup               # interactive wizard (hidden input for the token)
tmb telegram token               # set/change the bot token (hidden input, then getMe test)
tmb telegram test                # safe getMe-style verification, shows @username
tmb telegram admin-list | admin-add [ID] | admin-remove [ID]
tmb telegram support             # set support username
tmb telegram polling             # set polling timeout
tmb channels status|enable|disable|list|add|remove|test|update
tmb logger status|enable|disable|list|add|remove|alerts|mirror|payment-events|attestation|health
tmb cookies status|replace [FILE]
tmb sessions                     # authentication surface status
```

Configuration changes are made through the typed application CLI (`telegram-media-bot
config-edit`), which validates the complete result, writes atomically, preserves unrelated keys,
creates a rollback copy, and keeps restrictive permissions. The token is never echoed and never
printed afterward. The bot token is stored only in `config.yaml`; secrets never enter `.env`,
Docker image layers, or logs.

## Local Bot API

```bash
tmb local-api status
tmb local-api configure          # api_id / api_hash (hidden) / mode / port
tmb local-api start | stop | restart
tmb local-api migrate-to-local   # Cloud -> Local (explicit, fail-closed)
tmb local-api migrate-to-cloud   # Local -> Cloud
```

The manager drives the application's existing migration state machine; it never bypasses
endpoint leases, logout uncertainty, Telegram's cloud reuse waiting period, or the
bot/worker stop requirements. `api_hash` is never printed.

## Uninstall

```bash
tmb uninstall                    # interactive menu
tmb uninstall --yes              # remove containers only, keep data (non-interactive)
tmb uninstall full --yes         # full uninstall (requires exact confirmation)
```

Full uninstall offers to create a migration backup first and requires the exact phrase
`DELETE-FULL-UNINSTALL` (or `--yes` after review). Unrelated Docker resources are never removed.

## Non-interactive operation

All of the above commands work without a terminal. Destructive operations require `--yes` or an
exact typed confirmation; they never succeed silently just because stdin is not a TTY. Read-only
commands (`status`, `logs`, `storage`, `backup list/verify/inspect`, `doctor`) never take the
management lock and remain available while a mutating operation runs.

## Files and layout

- `scripts/tmb.sh` — entrypoint: path resolution, dispatch, menus.
- `scripts/lib/common.sh` — helpers, lock, redaction, sizes.
- `scripts/lib/services.sh` — compose lifecycle and health verification.
- `scripts/lib/update.sh` — transactional verified updater.
- `scripts/lib/backup.sh` — consistent backups, manifest, checksum, listing.
- `scripts/lib/restore.sh` — transactional restore/migration import with rollback.
- `scripts/lib/status.sh` — dashboard.
- `scripts/lib/storage.sh` — storage overview and bounded cleanup.
- `scripts/lib/docker.sh` — project-scoped Docker helpers.
- `scripts/lib/logs.sh` — log browsing and the support bundle.
- `scripts/lib/telegram.sh` — Telegram/cookies/logger wizard frontends.
- `scripts/lib/diagnostics.sh` — doctor and database health.
- `scripts/lib/config.sh` — configuration frontend over the typed CLI.
- `scripts/lib/ui.sh` — menu/color helpers.