# Instagram session admission and incident response

Issue [#4](https://github.com/HamedSanaei/telegram-media-downloader-bot/issues/4) reports account
restrictions after a few downloads and an unexpected account email change. Neither this change nor
the source inspection establishes what Instagram detected or who changed the email. Treat the email
change as a separate possible account compromise. No production account was load-tested for this fix.

## Admission policy

All worker Instagram extraction entry points share one admission service: gallery inspection,
downloads and Highlight trays; yt-dlp inspection, carousel resolution and each video-child download.
Stories and Highlights receive the same protection. Other providers do not consume this budget.

The shared operator cookie has one opaque session identity across Telegram users and worker
processes. Identity follows its Instagram session record, not a pathname, CSRF metadata, expiry
change or unrelated provider cookies. Personal identities are isolated by owner and credential
generation; a temporary lease path never changes that identity. Anonymous Instagram traffic has a
separate global identity. No cookie value, username or raw Telegram ID enters Redis admission keys.

The default `media.instagram.admission` settings are:

| Setting | Default | Meaning |
| --- | --- | --- |
| `min_spacing_seconds` | 30 | Minimum interval between operation starts for the same credential |
| `burst_window_seconds` | 3600 | Rolling short budget window |
| `burst_operations` | 6 | Maximum operations in that window |
| `daily_operations` | 30 | Maximum operations in a rolling 24 hours |
| `rate_limit_cooldown_seconds` | 3600 | Minimum pause after a rate limit or provider Retry-After |
| `max_wait_seconds` | 120 | Maximum worker wait for spacing or an occupied credential |

One active operation per credential is enforced. Redis `SET NX EX` and token-checked Lua release
coordinate workers. SQLite/WAL atomically records active operations, quota charges and pauses as
durable truth. All supported worker processes must share the same state database and Redis;
multi-host SQLite deployments are still unsupported. Restarting/flushing Redis cannot erase quotas
or provider pauses. Losing either backend fails closed for Instagram.

These are **extractor-operation budgets, not HTTP-request quotas**. One operation can make several
provider requests, especially a large Story/Highlight collection. Existing item/time/size bounds and
gallery request sleeps still apply. Direct Instagram yt-dlp retries are disabled, fragments are
serial and extractor requests are spaced. Existing gallery/nested yt-dlp retries remain disabled.
Do not increase collection bounds to circumvent the budget. Datacenter IP reputation and automated
access may still trigger restrictions at conservative traffic levels; this reduces risk and does
not guarantee account safety or availability.

## Inspection, retry, fallback and rotation

A typical UI inspection followed by a gallery download costs three operations: the initial
inspection, the fresh authorized download-job inspection and transfer. Native video-child discovery
and each transfer are additional operations. The existing same-attempt inspection reuse remains;
durable UI selections are not an authorization or credential cache. Owner binding, generation,
lease, private entitlement and expiry checks remain authoritative.

HTTP 429 pauses the entire credential for the greater of the configured minimum and the observed
Retry-After. Deadlines over the worker's previous 900-second retry cap are retained. Explicit
challenge/checkpoint or reauthentication requires manual review with no automatic expiry. Invalid
provider deadlines also require review. Any positive observed Retry-After pauses that credential,
including on an otherwise non-retryable denial. No automatic probes are scheduled.

The failed operation and newly queued operations return a controlled pause response without making
another provider request or switching credentials. The vault's connected state does not override
this separate operational circuit. Local pre-extraction credential expiry/disconnect can still use
the existing public-only fallback; the operator credential must pass its own gate. An observed
provider authentication failure cannot trigger that fallback. Private requests remain USER_ONLY.
This narrows the older unconditional typed-auth fallback described in ADR-047/ADR-050.

Operator cookie snapshots are private temporary files under the active job root, separate from
media output. Both identity derivation and extraction use the same snapshot, even during an atomic
administrator cookie upload. Finalizers remove snapshots, and existing terminal/orphan job cleanup
recovers them after a process crash. Source cookie files remain read-only. A legitimate new operator
session or personal credential generation has a new identity; old pause/history rows remain. Never
rotate accounts, sessions or IP addresses merely to bypass a provider restriction.

## Operator visibility and recovery

From the project directory, inspect locally without provider traffic:

```bash
docker compose exec worker telegram-media-bot instagram-admission-status
```

Output contains the current operator's opaque identity and per-identity kind, circuit/quota reason,
pause expiry (UTC epoch seconds; null means manual review), active flag, recent/day operation counts
and currently waiting admission operations. Waiting counts expire after the bounded admission wait;
they do not claim to enumerate ARQ jobs whose credentials have not yet been resolved. Existing queue
depth metrics report total outstanding queued work. State history can include older generations.
If the canonical cookie file is missing or malformed, identity is null with a fixed unavailable
state; durable history remains visible and extraction still fails closed.

The worker `/ready` detail includes aggregate credentials/paused/active/waiting state without making
other providers unready. Prometheus exposes `media_bot_instagram_admission_total` with only fixed
outcome/reason labels, plus aggregate paused/active/waiting gauges. Identity is never a metric label.
Structured logs contain fixed admission decisions/reasons. New circuit transitions emit sanitized
alerts through the configured Operator Logger; there is no administrator-DM fallback or repeated
pause alert on every denied job.

A worker crash deliberately leaves the credential active/quarantined. A Redis lease expiry alone
cannot prove the old extractor has stopped. After investigating the incident and stopping **all**
workers, clear only the reviewed identity, then restart workers:

```bash
docker compose stop worker
docker compose run --rm --no-deps worker telegram-media-bot instagram-admission-review OPAQUE_ID --workers-stopped
docker compose up -d worker
```

Stop every worker instance if there are several. The flag is an explicit operator assertion, not a
remote process detector. Review clears the stale Redis permit, manual circuit and interrupted
active marker, but preserves operation history and spacing. It refuses an unexpired timed provider
or quota pause; it never shortens Retry-After. Review does not replay failed jobs, validate cookies
against Instagram, or grant private-media authorization. If coordination is unavailable, recovery
fails closed. To roll back code, preserve the database; additive admission tables do not change
existing jobs, receipts or vault rows. Older code cannot enforce this new protection: keep Instagram
traffic stopped until a protected version is restored.

## Suspected account compromise

1. Stop use of the suspect cookie and all affected jobs. Preserve sanitized timestamps and incident
   notes; do not post cookies, signed URLs, passwords or session exports in an issue or logger.
2. Through Instagram's own trusted interface, inspect login activity, account email/password
   changes and security notifications. Secure the email account as well. Invalidate old sessions
   and enable two-factor authentication. Complete any checkpoint through the provider itself.
3. Inspect permissions and exposure of private bind mounts, configuration, cookie/vault backups,
   shell history and logs without printing secret contents. Limit host and backup access. Rotate
   genuinely exposed credentials after securing the account, not as a restriction workaround.
4. Resume only after review and the full provider deadline, using a small authorized normal request.
   Do not stress-test the affected real account. An unexpected email change must be investigated
   independently; a 429 alone is not evidence of a compromise or of its cause.
