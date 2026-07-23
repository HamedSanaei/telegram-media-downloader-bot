# Ready-to-paste Codex prompt

Read `AGENTS.md` first and treat it as binding. Then read every file under `docs/`, especially
`PROJECT_SPEC.md`, `ARCHITECTURE.md`, `ROADMAP.md`, `ACCEPTANCE_CRITERIA.md`, `STATUS.md`, and all
`docs/tasks/T*.md` files.

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
   known limitations, and the next recommended release step.

Do not ask for confirmation between tasks. Make reasonable decisions consistent with the existing
specification and record important decisions in `docs/DECISIONS.md`.
