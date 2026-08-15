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
  uploaded filename, and summaries expose only service labels plus replaced/added record counts.
  The next yt-dlp or gallery-dl job opens that same effective file; no process restart is needed.

No command returns URLs, tokens, cookies, proxy data, internal exception text, or file paths.
The explicit full-cookie download is the sole exception and is restricted to a current
administrator in a private bot chat; operators must delete or secure the resulting Telegram
document according to their credential policy.

Each final inspection/download failure and every `delivery_uncertain` result is also sent privately
to every unique configured administrator. Intermediate retries, cancellations, and successful jobs
do not generate alerts. Alerts contain only the opaque job ID, job kind, normalized source,
terminal status, stable error category, and attempt number. Each administrator must have opened the
bot's private chat at least once; a blocked/unreachable administrator is counted in a redacted
worker warning without preventing delivery to other administrators or changing the job outcome.
Use `/failed` as the durable fallback because alert messages themselves are intentionally not
stored or replayed.

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
