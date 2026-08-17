---
name: repo-navigation
description: Route unfamiliar or cross-component work in this repository with Graphify and compact indexes. Use when locating symbols, callers, dependencies, tests, subsystem ownership, or the smallest authoritative working set before editing.
---

# Repository navigation

1. Read and obey root `AGENTS.md`; understand the task before searching.
2. If ownership is unclear, use `docs/agent/CONTEXT_INDEX.md`.
3. Confirm Graphify freshness, then run a bounded `graphify query "..." --budget 1200`; use
   `graphify explain` or `graphify path` only to narrow the result.
4. If Graphify is unavailable, run `python scripts/agent_context.py symbol|refs|tests ...`.
5. Read the identified source and relevant tests. For architecture-sensitive work, also load the
   detailed architecture/ADR/spec routed by `docs/agent/DECISION_INDEX.md`.

Graphify answers where and what is connected. Source defines behavior, tests define expected
behavior, and detailed docs explain why. Never dump the complete graph/report into context.

