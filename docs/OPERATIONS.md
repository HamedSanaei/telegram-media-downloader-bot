# Operations

## Start and validate

```bash
./manage.sh init
# edit config.yaml and protect it with mode 0600
./manage.sh config-check
./manage.sh up
./manage.sh status
```

Windows equivalents use `manage.ps1`. The stack contains bot, worker, and persistent Redis. SQLite,
downloads, temporary files, upgrade reports, and cookies live below `./data`. Configuration and
cookies are sensitive; the managed update backup also captures the durable SQLite and Local Bot API
state described below.

Production operators can instead use the Docker-first one-line installers and the shared `tmb`
menu/command documented in `docs/INSTALLATION.md`. The release topology additionally runs the
official pinned Local Bot API in a dedicated Compose service.

## Service lifecycle and automatic recovery

Every production service (`bot`, `worker`, `local-api`, `redis`) runs with the Compose restart
policy `unless-stopped`, because an always-on Telegram bot must survive Docker daemon restarts
and server reboots. Docker owns recovery in every case:

- **Crash recovery** — a container that exits is restarted automatically.
- **Daemon/host restart** — every service that was running is restored when Docker comes back;
  the stack never ends up with only Redis online.
- **Explicit `tmb stop` / `tmb uninstall`** — services are intentionally stopped (`compose down`)
  and are never resurrected by the policy.

`tmb restart` recreates containers explicitly and is unaffected. The release updater stops only
running application writers with `compose stop` (Redis stays online), which marks them
intentionally stopped, so the policy does not race the update transaction; failed transactions
still restore the exact prior service set. Health checks, startup readiness, and update health
verification are unchanged — the restart policy is not a replacement for them.

### Durable updates and job recovery

Telegram updates are durably journaled to SQLite before their offset is acknowledged, so replayable
work received while the bot is offline or when the process dies mid-handling survives a restart.
Completed updates are never replayed and duplicate deliveries are ignored. Updates are persisted as
safe JSON using aiogram's own Telegram-object serializer (never raw ``Update.model_dump_json()``,
which cannot handle aiogram ``Default`` sentinels). The serializer is called without the real Bot's
outbound defaults, so parse mode, link-preview, content-protection, and caption-placement defaults
are not injected into inbound snapshots; stored JSON round-trips to the inbound handler semantics.

Serialization and persistence proceed sequentially in Telegram update order. The first unresolved
serialization failure is a hard batch barrier: no later update in that batch is persisted,
processed, or acknowledged, and the next ``getUpdates`` request uses the failed update ID as its
offset. Once that update serializes and persists, ordered processing resumes. After the bounded
failure threshold, the update ID and failure are durably recorded as a non-replayable terminal
tombstone with a sanitized marker payload. That tombstone is audit evidence, not preservation of the
original update: handler processing is deliberately abandoned so one impossible update cannot block
all subsequent traffic forever. Existing TERMINAL_FAILURE retention applies unchanged.

Supported-provider download failures classified as recoverable (expired/invalid cookies, or an
app/runtime bug later fixed in a new release) can resume automatically: replacing a provider's
cookie requeues that provider's eligible auth-failed jobs, and an app-fix failure gets one bounded
retry when a newer version is deployed. Unsupported sources (e.g. Pornhub and other non-supported
sites), cancelled jobs, and `delivery_uncertain` jobs are never replayed automatically — the
`/resolve` operator flow still owns those.

Recovery is gradual, never a burst. Cookie/app remediation requeues one bounded batch per pass
(default 20 jobs, oldest-first with a per-user cap), and the existing maintenance job keeps
draining the backlog in later passes until it is exhausted — the administrator does not re-upload
the cookie repeatedly. When the live queue depth is at or above
`operations.recovery.queue_pressure_threshold`, recovery defers to a later pass. SQLite remains the
source of truth: if Redis is down when a requeue is committed, startup reconciliation re-enqueues
it once; duplicate ARQ submissions cannot duplicate delivery. Bounded attempts (`max_recovery_attempts`)
and a max age (`max_recoverable_age_days`) prevent infinite loops.

Durable Telegram updates are not kept forever. COMPLETED inbox history is purged after
`operations.inbound_updates.completed_retention_days` (default 14), TERMINAL_FAILURE after
`terminal_failure_retention_days` (default 30), in batches of at most `cleanup_batch_size` (default
500) per maintenance pass. RECEIVED/PROCESSING updates are never age-purged — they may be
unfinished user work — and instead surface as `inbound_updates_stuck` (metric + worker log) when
older than `stuck_after_minutes`.

A reserved status effect that remains PENDING beyond
`operations.inbound_updates.effect_pending_stale_minutes` (default 10 minutes) is quarantined as
UNCERTAIN during startup and maintenance. This is intentionally not retried: SQLite cannot know
whether Telegram received the request before a crash, so avoiding duplicate visible messages is
safer than replaying uncertain cosmetic status. The side-effect ledger (inspection status, Story
delivery-mode prompt, recovery notices) is purged after `effect_retention_days`; fresh PENDING
rows are never purged, and UNCERTAIN rows become purgeable only after stale reconciliation.

Recovery queue pressure is based on outstanding ARQ queue entries, not a fixed queue capacity.
ARQ's `queue.max_jobs` is worker concurrency (a semaphore bounding jobs running simultaneously).
The pressure probe reads `zcard` on the queue sorted set. Because ARQ uses pessimistic execution, a
job stays in that sorted set while waiting, while running, and while deferred/retried, and is
removed only at final success or failure — so `queue_depth()` counts **outstanding ARQ queue
entries** (waiting + running + deferred/retry), not a waiting-only backlog.

Unless `operations.recovery.queue_pressure_threshold` is explicitly set, the effective threshold is
`queue.max_jobs * operations.recovery.queue_backlog_per_worker_slot` (default multiplier 4). For
the default `queue.max_jobs = 3` that is a threshold of **12 outstanding entries total** — which
may include up to 3 running jobs plus remaining waiting/deferred work; the multiplier is a pressure
heuristic relative to worker concurrency, not a literal queue-capacity calculation. Automatic
historical recovery only fills spare headroom below that threshold — a batch is trimmed to
`threshold - current_outstanding_depth` — and is deferred entirely when depth reaches the
threshold. Fresh user requests keep their existing queue admission behavior and always have
priority over historical recovery. Startup and maintenance retry deferred recovery in later bounded
passes. Recovery enqueue reconciliation is durable-state repair (jobs already committed as QUEUED
in SQLite but missing from Redis) and is therefore not throttled, so it always converges and never
strands a job.

Durable update processing is at-least-once: an update replayed after a crash reuses the durable
job and the already-sent status message instead of duplicating them. Final media delivery is never
auto-retried through this mechanism; uncertain delivery remains `delivery_uncertain` and is owned
by `/resolve`.

Release rollback uses `TMB_RELEASE_TAG=vX.Y.Z tmb update`. The updater validates the full staged
Bash/Compose/config payload and pulls candidate images before stopping writers. It records all four
project services, stops only the running bot/worker/Local API writers, backs up state, installs
application entries through a rollback snapshot, repairs runtime ownership/modes, and requires
same-UID filesystem plus SQLite WAL probes and an explicit offline doctor. Redis and ARQ remain
online. Offline verification requires Python/package version, yt-dlp, gallery-dl, canonical
cookies, ffmpeg/ffprobe, Deno, Local API static configuration/filesystem/migration state,
chart/font resources, and 7-Zip when enabled; it never requires a stopped service or network API.
After restoration, Local API reachability is checked only if `local-api` was originally running,
and Telegram plus required channels only if `bot` was originally running. Candidate services must
be running/healthy and match the original service set exactly; backup, probe, offline/online
verification, crash/restart, timeout, or state mismatch restores the prior transaction and service
state. Diagnostics name the failed stage and expose only bounded redacted command output.
Backup archives exclude downloads/temp and the exact volatile Local API log, but include
configuration, `.env`, SQLite/WAL/SHM state, cookies, and other Local API state. Archives are private,
atomically published, and partial files are deleted; Redis continues in its persistent Compose
volume.

Release v1.3.7 is withdrawn and is not a valid install, update, or rollback target. The standalone
installers/updaters reject both `v1.3.7`/`1.3.7` requests and any checksummed candidate archive whose
package version is `1.3.7`. Requested-target rejection precedes network access; candidate rejection
precedes image pulls, writer stop, backup, source/configuration changes, and all persistent-state
changes. This is a target-only denylist, so a host currently running v1.3.7 can update normally to
v1.3.8 or a later allowed release. Operators must choose an explicitly healthy release rather than
expecting an automatic redirect. The canonical policy is `release-policy.json`; standalone script
snapshots are kept identical by tests, and release build/publication tooling fails closed on the
same policy.

An affected v1.2.1 installation must use the checksummed standalone v1.2.2 updater once because its
installed script cannot replace itself before the faulty old preflight. The exact verification and
execution sequence is documented in `docs/INSTALLATION.md`; no `gallery_dl` configuration edit is
required.

For v1.2.2 -> v1.3.0, use `TMB_RELEASE_TAG=v1.3.0 tmb update` after the release is published. A
deployment with only `yt_dlp.cookies_file: /data/cookies/cookies.txt` needs no cookie-path
migration. Divergent legacy `gallery_dl.cookies.*` files must be merged into that combined file and
their aliases made null or identical before the update; `config-check` rejects split cookie state.

For v1.3.0 -> v1.3.1, use the checksummed standalone v1.3.1 updater procedure in
`docs/INSTALLATION.md`. The installed v1.3.0 updater performs its vulnerable backup before it can
install new updater code, so an ordinary `tmb update` is not the supported bootstrap for this one
transition. After v1.3.1 is installed, normal pinned updates resume.

For v1.3.1 -> v1.3.2, use the checksummed standalone v1.3.2 updater procedure in
`docs/INSTALLATION.md`. The installed v1.3.1 runner cannot acquire the new offline/online phase
selection during its own transaction, so an ordinary update would reproduce the production failure
and roll back. No configuration or cookie-path migration is required.

A user cancellation is committed to SQLite as `cancelled` before ARQ abort is requested. Pending or
running queue work is aborted with ARQ's official API and finalized transient keys are removed.
Startup reconciliation converts legacy `cancel_requested=1` queued/running/retrying rows to
`cancelled`, cleans their job directories, and never enqueues them. Healthy abandoned `running`
jobs are still requeued, while interrupted delivery remains quarantined as `delivery_uncertain`.

If the Windows execution policy blocks unsigned local scripts, use
`powershell -NoProfile -ExecutionPolicy Bypass -File .\manage.ps1 COMMAND` for the current process or
apply the organization's approved signing policy; do not lower the machine-wide policy silently.

`./manage.sh doctor` prints ffmpeg, ffprobe, the configured JavaScript runtime, and the resolved
7-Zip executable version. It also prints `OK usage_chart_font` after decoding the package-bundled
Noto Sans resource and required ASCII glyphs, followed by `OK usage_chart_renderer` after an
in-memory PNG smoke render. A failure is actionable: reinstall or update the immutable application
image; do not install a host font package as a workaround. The
worker container exposes `/health`, `/ready`, and `/metrics` internally on port 8080 by default. The
port is intentionally not host-published; query it through the container/network or an authenticated
monitoring sidecar.

Managed/external Local Bot API installation, explicit public-to-local migration, the 10-minute cloud
rollback interval, and large-file validation are in `docs/LOCAL_BOT_API.md`. Always stop Bot and
Worker before a migration command; process leases enforce this rule.

Every result above the configured direct-upload ceiling is sent as stored ZIP volumes. There is no
Premium/Userbot service to operate. Full setup, extraction, and troubleshooting instructions are
in `docs/MULTIPART_DELIVERY.md`.

The status message and worker JSON logs report upload stage, part ordinal/count, bytes, percentage,
and elapsed time. After a part is streamed to Local Bot API, a 30-second heartbeat reports that
Telegram is still processing it; no synthetic percentage is shown. The default per-part timeout is
four hours. A network failure remains `delivery_uncertain`: check the chat, then use `/resolve`;
never retry it automatically.

FFmpeg JSON logs report PID, exit code, configured threads, elapsed/processed duration, speed,
approximate percentage, and current output size. Cancellation terminates the FFmpeg process group;
the worker-level concurrency gate and optional `TMB_WORKER_CPUS` quota protect shared hosts.

## Telegram administration

Only IDs in `telegram.admin_ids` can use:

- `/health`: Redis/database status and queue depth;
- `/queue`: durable and Redis queue counts;
- `/failed`: recent opaque job IDs and stable error categories;
- `/block USER_ID` and `/unblock USER_ID`: durable dynamic policy.
- `/resolve JOB_ID`: mark an operator-reviewed `delivery_uncertain` job terminal so a new request is
  permitted; it never resends automatically.
- **Cookie Management** in the private reply-keyboard panel: merge a bounded Netscape document into
  the canonical cookie file or download the complete current file. Service detection ignores the
  uploaded filename, and summaries expose only service labels, provider record counts, health
  state, and replaced/added counts. Session-only exports are honestly `UNVERIFIED` rather than
  `MISSING`; a successful response means canonical replacement and provider-scoped static refresh
  both completed.
  The next yt-dlp or gallery-dl job opens that same effective file; no process restart is needed.
- **Cookie Health** is deliberately passive/local. Opening or refreshing the panel, worker startup,
  and cookie upload only inspect the canonical file (existence/readability, Netscape structure,
  provider counts, expiry/session records, and permissions). They never contact Instagram or any
  other provider. `UNVERIFIED` means the file is structurally present but authentication has not
  been tested with the provider. AUTH_FAILED is learned only from an authentication error already
  returned by a real user-requested extraction; no follow-up diagnostic request is sent. The old
  watcher and live "check all" action no longer exist.
- Old `cookie_health.expiry_watch_interval_minutes`, `active_probe_interval_minutes`,
  `probe_timeout_seconds`, `probe_concurrency`, and `probes` keys are deprecated no-ops. Existing
  config files containing them continue to load; operators may remove them during routine cleanup.

No command returns URLs, tokens, cookies, proxy data, internal exception text, or file paths.
The explicit full-cookie download is the sole exception and is restricted to a current
administrator in a private bot chat; operators must delete or secure the resulting Telegram
document according to their credential policy.

Eligible terminal inspection/download failures, `delivery_uncertain`, and Cookie Health transitions
route to the durable Operator Logger outbox when its alert gate is enabled. They are not broadcast
to administrator private chats. Alerts contain only the opaque job ID, job kind, normalized source,
terminal status, stable error category, and attempt number. `/failed` remains the durable job-state
inspection path; logger delivery failure never changes the user job result.

## Operator Logger rollout and incident runbook

The logger is an optional, secondary subsystem. SQLite/WAL is its durable truth; Redis does not own
logger delivery. The worker claims at most 20 effects every 30 seconds. A pre-send restart safely
retries an expired lease, while an expired lease after send start becomes `UNCERTAIN` and is never
automatically resent. Disabling/removing/forbidding a destination terminalizes work that has not
crossed the send boundary; successful history and in-flight/uncertain evidence are preserved.

Before any activation, assign a rollout owner and UTC change window, review private-channel
membership (minimum humans), grant the bot only membership/posting access, and run `tmb backup`.
The managed backup temporarily stops the bot/worker/Local API writers, captures configuration,
`.env`, canonical cookies, and the whole `data/state` directory (including the SQLite database and
any WAL/SHM/logger rows), restores exactly the previously running services, and excludes download
and temporary workspaces. Protect the archive as credential-bearing material. Run `tmb doctor`,
`tmb status`, and the internal `/ready` and `/metrics` checks after service restoration.

Activate in this order, restarting after each reviewed configuration change:

1. Keep `telegram.logger.enabled`, `alerts_enabled`, and `submission_mirror_enabled` false; start the
   old database/configuration once to prove additive initialization and ordinary downloads.
2. Add/test the private destination from `🧾 کانال‌های لاگر` (or configure `channels`), confirm the
   probe proves channel type, bot membership, and posting permission, then enable the destination.
3. Set only `logger.enabled: true`; confirm the bounded dispatcher, safe health detail, and empty
   outbox gauges without admitting events.
4. Set `alerts_enabled: true`; verify one controlled terminal operational event and one Cookie
   Health transition reach the channel without any automatic admin DM.
5. Review the exact Persian notice and indefinite retention, set `operator_privacy_attested: true`,
   then set `submission_mirror_enabled: true`. Verify an unacknowledged user sees the notice before
   acceptance, acknowledgement creates no mirror, and the next accepted submission copies once.
6. Record the owner, activation time, backup archive, config diff, destination probe, metric
   baseline, and rollback criteria. T024 remains blocked; no payment/VIP credential event is enabled
   by this rollout, and release `1.3.7` remains forbidden.

Safe readiness detail is exposed as `operator_logger` with only configured/effective/health counts,
feature flags, outbox states, and oldest-pending age—never IDs, URLs, captions, usernames, or
secrets. Metrics are limited to delivery outcome/category and aggregate pending/uncertain/age
gauges. Recommended initial alerts are: any forbidden destination; logger enabled with zero active
destinations for 2 minutes; oldest pending age above 300 seconds or pending depth above 20 for 10
minutes; any `UNCERTAIN` effect; or monotonic terminal-effect growth over 15 minutes.

For permission loss or channel removal, disable the destination first, inspect the aggregate health
and worker event `audit_dispatch_completed`, repair membership/posting permission, use the admin
probe, then re-enable. Historical terminal/uncertain work is not replayed; generate a new controlled
test event. For outbox growth, check Telegram reachability, worker scheduling, disk/SQLite health,
and the destination probe. For `UNCERTAIN`, inspect the private channel and event identity, record a
manual resolution, and never reset it to retryable. For a privacy or secret incident, immediately
turn off both event gates, restrict channel membership, preserve SQLite/log/backup evidence, rotate
the affected credential outside the bot, and follow the organization's incident process; do not
delete user messages or durable state as first response.

Rollback sets `submission_mirror_enabled`, `alerts_enabled`, and then `logger.enabled` false and
restores the matching pre-change configuration. It does not delete destinations, acknowledgements,
events, outbox rows, or Telegram copies. Audit content and safe metadata remain indefinitely
retained with no automatic purge; any future manual purge requires its own bounded, idempotent,
independently retried design and approval.

Weekly and monthly administrator charts are generated entirely in memory with Pillow. The font is
`telegram_media_bot/assets/fonts/NotoSans-Regular.ttf`, licensed by the adjacent `OFL.txt`, and is
loaded through `importlib.resources`; Windows, Linux, Docker, and clean wheel installs therefore use
the same bytes.

For required channels, add the bot as administrator before enabling the policy. A user must be a
member of every configured channel. The “joined, recheck” button bypasses Redis cache; ordinary
checks use the configured positive/negative TTL.

## Alerts and diagnosis

Recommended starting alerts (tune after measuring normal traffic):

- `/ready` is non-200 for 2 consecutive minutes;
- queue depth exceeds `2 * queue.max_jobs` for 10 minutes;
- failure rate exceeds 10% over 15 minutes or regresses by more than 2 percentage points in canary;
- any `delivery_uncertain` record exists for more than 5 minutes;
- any Operator Logger destination is forbidden, any logger effect is uncertain, or the oldest
  pending logger effect exceeds 5 minutes;
- storage usage exceeds 80%, cleanup reports repeated failures, or no successful job is observed
  during a known-active traffic window.

Inspect `docker compose logs worker`, the stable error category, Redis health, free disk space,
`doctor`, then the SQLite job record. Never paste secrets or full user URLs into an incident ticket.
For uncertain delivery, check the target chat/operator evidence before deciding whether to submit a
new job; do not mutate it into an automatic retry.

If a usage chart has blank labels, run `tmb doctor` in the deployed image. Either chart check
failing means the package/image is incomplete or corrupt. Verify that the deployed version is
1.0.10 or newer and replace it through `tmb update`; never download a font during startup or mount
`/usr/share/fonts` into the container.

## Controlled yt-dlp update

## Controlled gallery-dl update

Gallery-dl never self-updates in a running bot or image. For each reviewed upgrade:

```bash
# 1. change only the exact gallery-dl version in pyproject.toml
uv lock
uv run pytest tests/unit/infrastructure/gallerydl
uv run python scripts/check_gallerydl_fixtures.py --check-installed-version
docker build -t telegram-media-downloader-bot:gallery-dl-canary .
```

Review the upstream changelog for extractor/event/output changes, inspect the lock diff for only
the intended package graph change, run all application gates and Docker smokes, then publish only
through the normal application release. The deterministic suite uses sanitized 1.32.8 fixtures;
live social contract URLs are opt-in and must contain no private account or committed secret.

The gallery-dl live contracts are independently opt-in. Point them at a local, untracked
configuration file and operator-maintained public single-item URLs:

```powershell
$env:RUN_GALLERYDL_CONTRACT_TESTS = "1"
$env:GALLERYDL_CONTRACT_CONFIG = "C:\secure\telegram-media-bot\config.yaml"
$env:CONTRACT_GALLERYDL_INSTAGRAM_URL = "https://www.instagram.com/p/<public-id>/"
$env:CONTRACT_GALLERYDL_TIKTOK_URL = "https://www.tiktok.com/@user/video/<public-id>"
$env:CONTRACT_GALLERYDL_TWITTER_URL = "https://x.com/user/status/<public-id>"
$env:CONTRACT_GALLERYDL_PINTEREST_URL = "https://www.pinterest.com/pin/<public-id>/"
uv run pytest tests/contracts/test_gallerydl_contract.py -m contract
```

Unset variables are skipped deliberately; deterministic CI uses the sanitized 1.32.8 fixtures and
never contacts social websites. Do not commit contract URLs, cookies, or the contract config.

For diagnosis, run `tmb config-check`, `tmb doctor`, and the fixture command above. Authentication,
expired cookies, rate limits, provider unavailability, vendor schema changes, invalid images, and
collection limits are separate stable categories. Logs never include cookies, signed URLs, raw
JSON, or full commands. Rollback means restoring both the previous exact dependency and `uv.lock`,
then rebuilding the previous application image; persistent config, SQLite/WAL, Redis, cookies,
downloads, and Local API state remain mounted and untouched.

## Supported Instagram URL classes (v1.3.3)

Instagram URLs are canonicalized before routing and share/tracking parameters (`igsh`, `utm_*`)
are stripped:

| URL shape | Class | Behavior |
| --- | --- | --- |
| `/p/SHORTCODE/` | Post | Gallery-dl images/carousel; mixed carousels keep the existing image/video split |
| `/reel/SHORTCODE/`, `/reels/SHORTCODE/`, `/tv/SHORTCODE/` | Reel | Gallery-dl video (`video_original`), original MP4 |
| `/stories/USERNAME/MEDIA_ID/` | Story | Gallery-dl exact story item; image stories use photo/file delivery, video stories use `video_original` |
| `/stories/USERNAME/` | Story account | Rejected as bulk; one exact story media id is required |
| `/USERNAME/` | Profile | Treated as a profile-avatar action; canonicalizes to `/USERNAME/avatar/`; never downloads the post history |
| `/USERNAME/avatar/` | Avatar | Gallery-dl avatar extractor; original JPEG delivered as photo or file/document |
| `/stories/highlights/ID/` | Highlight | Gallery-dl highlight extractor (existing) |

Cookies are required for private profiles and for most Stories; expired or login-required access
surfaces the dedicated authentication category, while an expired/deleted Story surfaces the
unavailable category. The canonical combined cookie file at `yt_dlp.cookies_file` is the single
source for all Instagram access.

## Controlled yt-dlp update

```bash
git switch -c chore/update-ytdlp
./manage.sh upgrade-ytdlp
git diff -- pyproject.toml uv.lock
./manage.sh check
docker build -t telegram-media-downloader-bot:canary .
```

The upgrade script records old/new versions in ignored `data/state/upgrade-reports/`, runs adapter
tests, and runs all configured source contracts only when `RUN_CONTRACT_TESTS=1`. Contract variables
are `CONTRACT_YOUTUBE_URL`, `CONTRACT_SOUNDCLOUD_URL`, `CONTRACT_INSTAGRAM_URL`,
`CONTRACT_TWITTER_URL`, `CONTRACT_PINTEREST_URL`, and `CONTRACT_TIKTOK_URL`.

Deploy the candidate to a staging bot/queue with a separate config and database. Export baseline and
canary counters as JSON with `jobs_total` and `failures_total`, then run:

```bash
./manage.sh canary-report baseline.json canary.json
```

Promotion requires the configured sample and regression threshold. No dependency bot may auto-merge
yt-dlp. For an emergency extractor breakage, use the same branch, adapter/contracts, full gates, and
shortened but nonzero canary; document the exception in the release record.

## Rollback and cleanup

Revert the dependency/release commit (including `uv.lock`) and rebuild the previous immutable image.
Never update packages inside a running container. `./manage.sh clean` removes only local download and
temporary job directories; it intentionally preserves SQLite history, configuration, cookies, and
Redis state.

## Native-only public video UI

The public bot exposes only `MP4 Native · H.264 + AAC` and `WebM Native · VP9 + Opus`. Generic MP4,
generic WEBM, and converted-video buttons do not exist. Old `container:`/`fmt:` callback payloads
are safe redirects to the current menu and never enqueue work.

MP4 Native is a low-latency path and never starts AV1/VP9-to-H.264 encoding.
`media.mp4_native_fallback: lower_resolution` selects the highest lower H.264/AAC resolution when
the requested height is available only as AV1/VP9; set it to `fail` to require an exact native
height. The option catalog deduplicates repeated fallback streams and displays the actual selected
resolution and selected-component size. Back navigation reuses the persisted inspection and creates
no Redis or SQLite work.

The resource-heavy converted-MP4 implementation remains internal even if
`media.transcode.explicit_mp4_enabled: true`. Rejections logged as
`transcode_rejected_timeout_estimate` are intentional preflight safety decisions; suggest fast MP4
at a lower resolution or Best Original instead of increasing the job timeout.
