# Personal Instagram connection activation: implementation and validation

## Outcome and scope

The installation's unavailable message was caused by disabled web connection and missing HTTPS
origin, signing/verifying keys and vault material. Administrator IDs do not activate this service.
The working-tree implementation now includes explicit safe provisioning, restricted web deployment
configuration and real login/2FA continuation fixes. It does not change media authorization,
public shared-cookie fallback, SQLite schema, dependencies or the published release version.

See [the operator runbook](../INSTAGRAM_CONNECTION.md) and ADR-049. The delivery, persistence and
release/upgrade checklists guided owner isolation, encrypted-state preservation and stopping the
additional SQLite writer before management transactions.

## Verification

- Exact `bash scripts/ci_fast_quality.sh`, using Git Bash on Windows and Python 3.14.5: passed.
  Includes frozen lock/sync, architecture/context/text/manifest, Ruff lint/format, mypy,
  tracked-file secret scan, dependency consistency and non-contract coverage.
- Windows non-contract tests: 1,753 passed, 12 platform/opt-in skips, 16 contract deselections;
  coverage 82.81%. ResourceWarning treated as an error.
- Linux non-contract tests: 1,760 passed, five platform/opt-in skips, 16 contract deselections;
  coverage 82.89%. ResourceWarning treated as an error. Node and PowerShell unavailable there;
  their relevant tests passed on Windows.
- An additional secret scan explicitly included every new untracked file: passed. Narrow inline
  annotations identify only synthetic password/rejected-URL fixtures, not runtime secrets.
- Clean wheel build/install/resource smoke: passed. Package metadata remains the unchanged local
  RC13 version; this build is not a new published release or a deployed production image.
- Both core and separate companion Compose definitions rendered successfully.
- ShellCheck with the complete CI input list: passed.
- Linux updater and Windows updater recovery fixtures: passed.
- Linux control-plane fixture: 155 passed, zero failed under a WSL PTY/job-control launch.
  No-PTY WSL execution had three SIGINT assertions fail identically on untouched HEAD; the PTY
  launch restored signal delivery without modifying or weakening those tests.
- Vulnerability audit: no known vulnerabilities found.
- After documentation and synthetic-fixture annotations, manifest generation/check, text/context
  integrity, Ruff, new-file secret scan, affected tests and `git diff --check` passed again.

No live Instagram account password/code was collected or tested. Only provider HTTP transport was
simulated in the end-to-end browser/service/crypto/vault integration fixture. The actual urllib
cookie transport and browser JavaScript were separately exercised without external login requests.
No media-adapter/dependency change was made; upstream network contract tests were not required.

## Rollout still required

Production diagnostics were read-only. The existing installation still needs explicit deployment
of an image containing these changes, private key/profile provisioning, trusted ingress routing,
companion start, and bot/worker recreation. The selected hostname resolves through a CDN but
currently repeats an HTTPS self-redirect. An unrelated service owns origin port 443; it was not
replaced or restarted. DNS/CDN/TLS routing must be resolved before inviting users to enter secrets.
Configuration-only readiness never implies that this external routing or real login has succeeded.

Changes remain uncommitted in the working tree. No commit, push, tag, publication, production
configuration mutation or service deployment was performed for this task.

## Graphify usage

- Version: `0.9.26`.
- Freshness: `graphify check-update .` exited successfully without output or a stale report before
  discovery. No `graphify update` was required/reported; fallback navigation was not used.
- Exact bounded discovery queries:

  ```bash
  graphify query "Trace personal Instagram connection disabled installation login session acquisition companion vault configuration and user login handlers" --budget 1200 --graph graphify-out/graph.json
  graphify query "Trace companion activation through installer management lock backup updater rollback and tests" --budget 1200 --graph graphify-out/graph.json
  ```

- Additional dependency inspection: `graphify explain SqliteJobRepository --graph graphify-out/graph.json`.
- Key discoveries: `CompanionInstagramConnectionFlow`, `CompanionWebApp`, `DisabledInstagramFlow`,
  `InstagramConnectionService`, `bootstrap/instagram.py`, `bootstrap/companion.py`,
  `SqliteJobRepository`, management backup/update entrypoints and their recovery tests.
- Both queries were budget-truncated; source code, tests and the relevant specification,
  architecture, decisions and task history were inspected as the actual behavior authorities.

## Follow-up: operator-authorized origin listener (2026-10-17)

The operator subsequently explicitly requested a random free HTTP origin port for the CDN panel.
A dedicated Nginx listener was installed under `/etc/nginx/conf.d/tmb-instagram-origin.conf` on
the selected host port, outside the ephemeral range, with a fixed loopback 8090 backend. No
application code, version, package or production bot configuration was changed by this follow-up.
No commit/push/publication was performed. Existing production containers were not restarted.

The listener uses the current ranges from `https://www.arvancloud.ir/fa/ips.txt`, checked by HTTPS
from both the developer environment and origin server. It also permits loopback diagnostics,
requires the selected Host and forwarded HTTPS indication, disables access logs/request-body
proxy buffering, and sends no-store responses. HTTP between edge and origin is an explicit
operator-selected risk; restricting edge source IPs does not encrypt this leg.

Validation: pre/post Nginx syntax checks and graceful reload passed; the chosen IPv4/IPv6 port
is listening. Local ingress health returned 200, the still-undeployed backend readiness returned
503, a request without forwarded HTTPS returned 403, and a direct public request outside the
edge allowlist returned 403. The public CDN route cannot be verified until the operator applies
the new origin settings. Syntax/reload failure recovery was prepared to move only the newly
added configuration out of active includes and reload the prior configuration.

The exact fast-quality lane was rerun after the follow-up documentation/manifest changes:
1,753 passed, 12 platform/opt-in skips, 16 contract deselections, 82.83% coverage, with
ResourceWarning treated as an error. The origin installation script also passed ShellCheck.

Graphify usage for this follow-up: version `0.9.26`; `graphify check-update .` succeeded with no
output/stale report; no update or fallback used. Exact bounded query:

```bash
graphify query "Trace web companion host port deployment reverse proxy and readiness" --budget 1200 --graph graphify-out/graph.json
```

Key discoveries were `CompanionWebApp`, `CompanionSettings`, `bootstrap/companion.py`, the
companion port contract and readiness entrypoints. The existing loopback Compose definition,
activation runbook, ADR-049 and actual server listener/configuration state were verified directly.

## Authorized RC14 release and rollout preparation (2026-10-17)

The operator explicitly requested push and deployment after the origin became reachable.
`1.4.0-rc.14` is the next prerelease; main/package/lock metadata is updated together without
changing dependency selections. Publication follows local validation and hosted CI; deployment
uses verified release assets, a private consistent backup, preserved keys/configuration, and
explicit companion activation after core upgrade. The current HTTP CDN origin choice remains
explicitly unencrypted on the origin leg. No real account login is claimed without user testing.

Graphify usage: `0.9.26`; freshness check succeeded without output/staleness, no graph update
required/reported, no fallback navigation. Exact bounded query for this phase:

```bash
graphify query "Trace personal Instagram companion release version upgrade backup rollback deployment and validation" --budget 1200 --graph graphify-out/graph.json
```

Key discoveries: `backup`, `rollback_update`, `CompanionWebApp`, `InstagramConnectionService`,
`bootstrap/companion.py`, and the updater/release entrypoints. Actual version assertions, archive
builder, CI/publication workflows, updater recovery fixtures, T011 and ADR-015/018/023 were read.

## Completed RC14 rollout (2026-10-17)

- Release commit/tag: `261248581511553685e5df9e1b9028a382d86ae5`, `v1.4.0-rc.14`.
- [Full application CI](https://github.com/HamedSanaei/telegram-media-downloader-bot/actions/runs/38993739924):
  all ten jobs succeeded, including `quality`, Docker runtime, both installers, privileged updater
  matrix and `final-ci-gate`. Hosted quality: 1,764 passed, one opt-in skip, 16 contract deselections,
  82.90% coverage. The release-version local suite passed 1,753 tests with 82.81% coverage.
- [Publication](https://github.com/HamedSanaei/telegram-media-downloader-bot/actions/runs/38994901268)
  and [prerelease](https://github.com/HamedSanaei/telegram-media-downloader-bot/releases/tag/v1.4.0-rc.14)
  completed successfully. Source archives reproduced locally and in publication. The stable latest
  release remains `v1.3.8`.
- Published image: `ghcr.io/hamedsanaei/telegram-media-downloader-bot:1.4.0-rc.14`, digest
  `sha256:7b1c5a12321559b7229b7ae96f4eab087e02a59f36261e1f547ca3e61f248f1d`.
  Its revision label matches the release commit. All four application containers use this image;
  Redis remains its separate unchanged service.
- Read-only image/configuration/doctor preflight passed on the actual server without changing
  running services. An isolated actual-image companion also booted non-root/read-only with no
  network/public port and a restricted profile; its readiness returned 200 and its temporary
  resources were removed afterward.
- The normal transactional updater preserved Redis, stopped only the filesystem writers and made
  the private mode-0600 `backups/tmb-20261017T024427Z.tar.gz` before application replacement.
  A separate `tmb-rollback:rc13-before-rc14-20261017024214` image reference protects the prior image.
- First activation failed closed because the operator helper mounted `/data` read-only for a
  SQLite WAL reader. Configuration was restored and the RC14 core remained healthy. The exact
  error was reproduced against a private configuration copy; the documented writable data bind
  made that same isolated setup succeed. No database rows were reset or deleted. Corrected
  activation preserved runtime sidecar ownership and passed configuration, service and public
  protocol verification. This was an operator-command correction, not a relaxed application gate.
- Successful private recovery directory: `backups/instagram-activation.XD4vzw`. Active main/profile
  recovery copies there are root-owned mode 0600 under a root-owned mode-0700 directory; live main
  and reduced profile remain mode 0600 for runtime UID 10001. No secret values appear in this report.
- Actual companion mounts are only `/app/companion.yaml` read-only and `/data/state` writable.
  Its active-writer management guard correctly refuses backup/update/restore writes until stopped.
- Public `/ready` and `/instagram/connect` return 200, including from the external developer host.
  A newly minted real signed handoff, sent through the public CDN route without logging its token,
  produced a Secure/HttpOnly/SameSite=Lax browser cookie, reached `need_credentials`, and rejected
  replay with 401. Telegram getMe/required-channel checks passed. Worker, Local API, Redis and
  companion are healthy; bot is running/reachable.
- No Instagram account password/code or upstream authentication request was used by deployment
  probes. The HTTP edge-to-origin leg remains an explicit operator choice and is not end-to-end
  TLS. Actual account authentication still needs the user's own secure-link interaction.
