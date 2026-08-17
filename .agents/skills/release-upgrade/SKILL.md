---
name: release-upgrade
description: Scope version bumps, dependencies, installers, Linux/Windows updater transactions, backup/rollback, release archives, Docker images, CI, or publication safeguards. Use for release preparation and production upgrade changes.
---

# Release and upgrade

Start with `graphify query "Trace VERSION_OR_UPDATE_CHANGE through version metadata, installer,
updater, rollback, archive builder, tests, CI, and publish workflow"`. Inspect `scripts/tmb.sh`,
the PowerShell counterpart when contracts overlap, updater fixtures, build/archive scripts,
workflows, version assertions, INSTALLATION/OPERATIONS, T011, and ADR-015/018/023.

Preserve pre-downtime validation, writer-stop backup ordering, Redis availability, private atomic
backups, exact service-state restoration, offline/online verification separation, sanitized
diagnostics, project-scoped cleanup, and full rollback. Keep lock/version/manifest/archive/image
metadata consistent. Run historical and privileged fixtures when applicable plus every release
quality gate. Never commit, push, tag, publish, or deploy unless the user explicitly authorizes it.

