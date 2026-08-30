# T011 - Controlled yt-dlp updates

**Status:** complete (2026-07-23)

The controlled upgrade script records locked old/new versions, runs adapter tests, optionally runs
all source contracts, and writes an ignored operator report with rollback steps. Contract fixtures
cover every example source. A failure-rate comparison gate and staging/canary runbook are included;
Renovate remains explicitly non-automerge.

The v1.0.3 hotfix also makes application release updates transactional: full staged-script and
Compose/config validation precedes service stop; runtime-user filesystem and SQLite WAL probes
precede startup; post-start health is mandatory; and post-stop failures restore the prior
application, image, usable permissions, command link, and service set. A privileged Docker
integration test exercises the published v1.0.2 updater against the v1.0.3 release archive.
Verified updates may subsequently remove only unused old images from the exact project repository;
the current/referenced images and all unrelated Docker resources remain protected. Operators can
preview the same allowlisted action with `tmb cleanup --dry-run`.

The v1.2.2 privileged regression derives its final filesystem and SQLite WAL probe from the exact
`APP_UID`/`APP_GID` consumed by Compose. It asserts the post-migration owner/mode of persistent
state, downloads, temp, cookies, Local Bot API state, and backups for both the v1.0.2 legacy updater
and the checksummed v1.2.1 standalone-updater bootstrap.

The v1.3.1 reliability patch makes the Linux backup itself a transaction boundary. Candidate
download/checksum/config/image work completes before downtime; only previously running
bot/worker/Local API filesystem writers stop; and Redis remains online. A mode-0600 temporary tar
is atomically renamed only after success, includes configuration/cookies/SQLite WAL state and
durable Local API state, excludes only the exact volatile Local API log, and never archives
downloads/temp. Backup failure restores the original service set without changing the installed
release. Offline version/doctor failures roll back application/image/permissions and the same exact
state. Mocked all-stopped/mixed-state tests and privileged v1.3.0 standalone-updater fixtures cover
active-log success, archive policy, rollback, and bounded secret-redacted diagnostics.

The v1.3.2 patch separates candidate/static and post-install offline verification from conditional
post-start online checks. Offline doctor remains fail-closed for package/dependencies/cookies/static
Local API and chart/runtime resources but never checks intentionally stopped services. After exact
restoration, only an originally running Local API and/or bot is online-verified. Privileged v1.3.0
fixtures cover the production all-running topology, both verification failure boundaries, bot or
Local API intentionally stopped, and mixed service states. v1.3.1 uses the checksummed v1.3.2
standalone updater once because its running updater contains the old phase contract.

## Deliverables

- Improve the upgrade script to record old/new versions and retain rollback instructions.
- Expand contract fixtures for every enabled source using operator-maintained safe URLs.
- Add a canary worker/profile or documented staging deployment.
- Compare failure rates before promotion.
- Never auto-merge yt-dlp updates.
- Document emergency upgrade and rollback procedures.

## Withdrawn-release follow-up

The v1.3.7 withdrawal adds a target-only release denylist shared by publication/archive tooling and
embedded in each standalone Linux/Windows installer/updater. Direct tag requests fail before
download and archive aliases fail on their verified package version before application, config,
state, image, backup, or service operations. Installed v1.3.7 remains allowed to recover forward to
v1.3.8 or later. Regression fixtures cover both tag spellings, candidate aliases, platform parity,
unchanged state, and future allowed releases.
