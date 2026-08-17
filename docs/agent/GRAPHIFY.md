# Graphify repository navigation

Graphify is the preferred structural navigation and dependency-discovery tool for this repository.
Source code is authoritative for behavior, tests are authoritative for expected behavior, and the
project spec/architecture/ADRs explain intent. Never implement from graph metadata alone.

## Supported integration

The integration was reviewed against Graphify/`graphifyy` 0.9.26. The external CLI is an isolated
developer tool, not a `pyproject.toml` dependency. Generated checkout-specific data lives under
ignored `graphify-out/`; `.graphifyignore` is the repository-scoped exclusion configuration. No
MCP/HTTP service, API key, Graphify install, or graph build is required by production or CI.

Install the reviewed CLI outside the application environment:

```bash
uv tool install graphifyy==0.9.26
graphify --version
```

Do not run `graphify install --project --platform codex`: the supported Codex installer writes to
`AGENTS.md`, whose established engineering contract must be preserved. This repository already
provides project-local navigation through `.agents/skills/repo-navigation/` and this guide.

## Build and freshness

Initial local, deterministic code index (Python, shell, PowerShell, tests, and other supported code;
documents stay authoritative outside the graph):

```bash
graphify extract . --code-only --no-viz
```

Before relying on the graph, check the current checkout. After structural edits, update it. Before a
final blast-radius conclusion, ensure every changed source/test/script has been indexed:

```bash
graphify check-update .
graphify update .
```

Graphify refuses some incomplete/shrinking overwrites by design. Investigate the cause; do not use
partial/force flags merely to silence freshness failures. Source code always wins over stale graph
metadata. Do not preload `graphify-out/GRAPH_REPORT.md` or `graph.json`; use bounded queries.

## Query-first workflow

```bash
graphify query "QUESTION" --budget 1200 --graph graphify-out/graph.json
graphify explain "SYMBOL" --graph graphify-out/graph.json
graphify path "SOURCE" "TARGET" --graph graphify-out/graph.json
```

Use results to choose a small working set, then inspect those source files, their relevant tests,
and the ADR/docs routed by `CONTEXT_INDEX.md` and `DECISION_INDEX.md`. If Graphify is absent or stale,
use the deterministic fallback:

```bash
python scripts/agent_context.py overview src
python scripts/agent_context.py symbol SYMBOL
python scripts/agent_context.py imports PATH
python scripts/agent_context.py reverse-imports PATH
python scripts/agent_context.py refs SYMBOL
python scripts/agent_context.py tests PATH_OR_SYMBOL
```

## Repository examples

Instagram Story entry, inspection, engine, delivery, and tests:

```bash
graphify query "Trace an exact Instagram Story URL from Telegram entry through canonicalization, inspection, media engine routing, and delivery; include tests" --budget 1400
graphify path "canonicalize_media_url" "GalleryDlEngine"
```

Local Telegram Bot API startup/readiness, callers, timeout, and tests:

```bash
graphify query "Where is Local Bot API startup readiness implemented, who calls it, and which timeout/failure tests cover it?" --budget 1200
graphify explain "wait_for_local_api_readiness"
```

Cancellation across Telegram, ARQ, worker, SQLite, subprocesses, and tests:

```bash
graphify query "Trace durable cancellation from Telegram callback through ARQ, worker, SQLite transition, yt-dlp/gallery-dl/ffmpeg termination, and tests" --budget 1600
graphify path "cancel_job" "SqliteJobRepository"
```

SQLite semantic change blast radius:

```bash
graphify query "Show reverse dependencies and tests for SqliteJobRepository job transitions and delivery receipts" --budget 1400
graphify explain "SqliteJobRepository"
```

Native yt-dlp selection, callers, configuration, and tests:

```bash
graphify query "Trace native yt-dlp format selection from configuration and inspection to persisted option and download validation; include tests" --budget 1400
graphify path "bounded_format_selector" "build_native_option_catalog"
```

Release/update path:

```bash
graphify query "Trace the Linux/Windows installer, updater verification and rollback, release archive builder, and privileged release tests" --budget 1600
graphify explain "perform_update"
```

Shell functions may be represented differently by the installed parser. If `explain` cannot resolve
one, use the natural-language `query`, then verify the exact shell source and tests. The code-only
index deliberately does not replace inspection of `pyproject.toml`, version assertions, CI/release
workflow YAML, or operations documentation; route those with `CONTEXT_INDEX.md`.
