# T011 - Controlled yt-dlp updates

**Status:** complete (2026-07-23)

The controlled upgrade script records locked old/new versions, runs adapter tests, optionally runs
all source contracts, and writes an ignored operator report with rollback steps. Contract fixtures
cover every example source. A failure-rate comparison gate and staging/canary runbook are included;
Renovate remains explicitly non-automerge.

## Deliverables

- Improve the upgrade script to record old/new versions and retain rollback instructions.
- Expand contract fixtures for every enabled source using operator-maintained safe URLs.
- Add a canary worker/profile or documented staging deployment.
- Compare failure rates before promotion.
- Never auto-merge yt-dlp updates.
- Document emergency upgrade and rollback procedures.
