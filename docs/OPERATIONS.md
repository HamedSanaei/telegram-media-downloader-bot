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
downloads, temporary files, upgrade reports, and cookies live below `./data`; only configuration and
cookies should be backed up as sensitive material.

If the Windows execution policy blocks unsigned local scripts, use
`powershell -NoProfile -ExecutionPolicy Bypass -File .\manage.ps1 COMMAND` for the current process or
apply the organization's approved signing policy; do not lower the machine-wide policy silently.

`./manage.sh doctor` prints ffmpeg, ffprobe, and the configured JavaScript runtime versions. The
worker container exposes `/health`, `/ready`, and `/metrics` internally on port 8080 by default. The
port is intentionally not host-published; query it through the container/network or an authenticated
monitoring sidecar.

## Telegram administration

Only IDs in `telegram.admin_ids` can use:

- `/health`: Redis/database status and queue depth;
- `/queue`: durable and Redis queue counts;
- `/failed`: recent opaque job IDs and stable error categories;
- `/block USER_ID` and `/unblock USER_ID`: durable dynamic policy.
- `/resolve JOB_ID`: mark an operator-reviewed `delivery_uncertain` job terminal so a new request is
  permitted; it never resends automatically.

No command returns URLs, tokens, cookies, proxy data, internal exception text, or file paths.

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
