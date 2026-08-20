---
name: repo-navigation
description: Route unfamiliar or cross-component work in this repository with Graphify and compact indexes. Use when locating symbols, callers, dependencies, tests, subsystem ownership, or the smallest authoritative working set before editing.
---

# Repository navigation

1. Read and obey root `AGENTS.md`; understand the task before searching.
2. If ownership is unclear, use `docs/agent/CONTEXT_INDEX.md`.
3. Follow the mandatory Graphify workflow below before broad grep/file exploration.
4. Read the identified source and relevant tests. For architecture-sensitive work, also load the
   detailed architecture/ADR/spec routed by `docs/agent/DECISION_INDEX.md`.

## Mandatory Graphify workflow (every non-trivial code task)

1. Verify Graphify is available: `graphify --version`; record the version or "unavailable".
2. Check graph freshness: `graphify check-update .`
3. If stale and Graphify is available: `graphify update .`; record that an update was required.
4. Run at least one bounded query relevant to the task before broad grep/file exploration:
   `graphify query "..." --budget 1200 --graph graphify-out/graph.json`
5. Use `graphify explain` or `graphify path` only when useful for narrowing dependencies or
   blast radius.
6. Graphify is discovery tooling only; actual source code and tests remain authoritative.
7. If Graphify is unavailable or fails, explicitly record that fact and use the dependency-free
   `python scripts/agent_context.py ...` fallback.
8. The final task report MUST include a `Graphify usage` section listing: graphify version,
   freshness result, exact bounded queries used, key symbols/files discovered, whether
   `graphify update` was required, and whether fallback navigation was used.
9. Do not dump `graphify-out/graph.json` or `graphify-out/GRAPH_REPORT.md` into model context.
10. Keep Graphify outside application dependencies and production/CI runtime; it is a local
    developer tool only, never a runtime or CI dependency.

Graphify answers where and what is connected. Source defines behavior, tests define expected
behavior, and detailed docs explain why. Never dump the complete graph/report into context.
