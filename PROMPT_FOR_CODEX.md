# Ready-to-paste Codex prompt

Read `AGENTS.md` first and treat it as binding. Understand the requested work before discovery.
Use the relevant Agent Skill under `.agents/skills/`; if subsystem ownership is unclear, route with
`docs/agent/CONTEXT_INDEX.md`.

### Mandatory Graphify workflow (every non-trivial code task)

Before broad grep/file exploration, follow these steps in order and record the results so that
Graphify usage is auditable:

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

Then inspect the actual source and relevant tests. Read the applicable detailed
project-spec, architecture, ADR, status, and task sections whenever the change touches their
contract. Graphify is navigation, not a substitute for those authorities.

Implement the remaining project in one coherent pass, following tasks in numerical order. Do not
rewrite the architecture or allow `yt_dlp` imports outside `infrastructure/ytdlp`. Preserve the
single local YAML configuration model, Docker Compose one-command startup, separate bot and
worker processes, typed project-owned contracts, safe file handling, controlled yt-dlp
upgrade workflow, and the Python 3.14-or-newer runtime policy. Do not downgrade Python.

Requirements for your final result:

1. Complete every task that is not marked complete in `docs/STATUS.md`.
2. Do not stop after scaffolding or planning; implement, test, and document the behavior.
3. Add no hardcoded per-site download handlers when the generic yt-dlp engine can handle them.
4. Keep Spotify and unsupported-source resolution outside the first generic engine unless the
   relevant task explicitly requires a separate adapter.
5. Run all commands listed in the Testing gates section of `AGENTS.md`.
6. Run contract tests only when `RUN_CONTRACT_TESTS=1` and document whether they were executed.
7. Update `docs/STATUS.md`, `docs/CODE_MAP.md`, and any ADRs before finishing.
8. Report changed files, implementation summary, exact test results, coverage, security checks,
   known limitations, the mandatory `Graphify usage` section (version, freshness result, exact
   bounded queries, key symbols/files discovered, whether `graphify update` was required, and
   whether fallback navigation was used), and the next recommended release step.

Recommended working sequence:

1. Read and obey root `AGENTS.md`.
2. Understand the task and inspect the working tree.
3. Use the relevant Agent Skill and `CONTEXT_INDEX` when routing is unclear.
4. Follow the mandatory Graphify workflow: verify availability, check freshness, update if stale,
   and run a bounded query for symbols, relationships, blast radius, and candidate tests.
5. Read the actual source, relevant tests, and applicable authoritative docs/ADRs.
6. Implement and run targeted tests during development.
7. Run every required final quality gate from `AGENTS.md`.

Do not ask for confirmation between tasks. Make reasonable decisions consistent with the existing
specification and record important decisions in `docs/DECISIONS.md`.
