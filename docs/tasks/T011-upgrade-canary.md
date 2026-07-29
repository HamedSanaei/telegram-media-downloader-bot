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

## Deliverables

- Improve the upgrade script to record old/new versions and retain rollback instructions.
- Expand contract fixtures for every enabled source using operator-maintained safe URLs.
- Add a canary worker/profile or documented staging deployment.
- Compare failure rates before promotion.
- Never auto-merge yt-dlp updates.
- Document emergency upgrade and rollback procedures.
