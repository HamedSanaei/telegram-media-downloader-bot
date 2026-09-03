# AGENTS.md

## Mission

Build and maintain a reliable Telegram media downloader bot powered by `yt-dlp` while keeping
all application code insulated from `yt-dlp` internals. The project must remain easy to update,
test, deploy, roll back, and hand over to another engineer or coding agent.

## Git authorship and contributor policy

- Never add an AI assistant, coding agent, automation tool, or bot as a Git author, committer,
  co-author, signer, or contributor.
- Never add `Co-Authored-By`, `Signed-off-by`, generated-by, assisted-by, or similar attribution
  trailers for AI, tools, or bots.
- Never change the Git author or committer identity to an AI or tool account.
- Commits must use only the configured human developer Git identity unless the user explicitly
  requests another human contributor.
- Do not append AI or tool branding or attribution to commit messages.
- Before every commit, inspect the complete commit message and author/committer metadata and remove
  accidental AI or tool attribution.

### AI / coding-agent attribution is strictly forbidden

No AI agent, coding agent, automation assistant, editor assistant, or tool may EVER appear as:

- Git author;
- Git committer;
- co-author;
- contributor;
- signer;
- PR author attribution text;
- generated-by attribution;
- assisted-by attribution;
- commit-message attribution;
- release-note attribution;
- source-code attribution;
- documentation attribution.

This explicitly includes, but is not limited to: Codebuff, Codex, ChatGPT, OpenAI, Claude,
Anthropic, Cursor, Copilot, Gemini, and any other coding agent or AI tool. Never add lines such
as `Co-Authored-By:`, `Generated-By:`, `Generated with:`, `Assisted-By:`, `Created by Codebuff`,
`Implemented by Codebuff`, or any other agent branding. No agent name may appear in source
comments, documentation, commit messages, or release notes. No agent may add itself as a
contributor. Only the configured HUMAN Git identity may be used.

Before ANY future commit, the committing party MUST check:

```bash
git config user.name
git config user.email
git log -1 --format=fuller
```

and inspect the complete commit message plus author/committer metadata for AI/tool attribution,
removing any accidental attribution before committing. AI-generated code, tests, and documentation
must carry no attribution whatsoever.

## Mandatory first steps for every task

1. Read this file completely.
2. Understand the task and inspect the existing working tree before deciding scope.
3. Use the relevant repository Skill under `.agents/skills/`. If ownership is unclear, consult
   `docs/agent/CONTEXT_INDEX.md`; before broad grep/file exploration, prefer a fresh Graphify query
   to identify the smallest relevant working set.
4. Inspect the relevant source code and tests before proposing or making changes.
5. Read the relevant sections of `docs/PROJECT_SPEC.md`, `docs/ARCHITECTURE.md`,
   `docs/DECISIONS.md`, `docs/CODE_MAP.md`, `docs/STATUS.md`, and task history as required by the
   change. For architecture-wide, persistence, queue, cancellation, concurrency, security,
   cleanup, upgrade/rollback, backward-compatibility, public-configuration, data-integrity, or
   release work, err on the side of loading more authoritative context rather than less.
6. Preserve the architecture invariants below.
7. Update `docs/STATUS.md` and `docs/CODE_MAP.md` whenever behavior or file ownership changes.

## Progressive repository navigation

- Graphify is discovery tooling only: use it for symbol location, relationships, dependency paths,
  blast radius, and candidate tests. Verify exact behavior in source code and tests before editing.
- Source code is the behavior authority, tests define expected behavior, and the project spec,
  architecture, ADRs, and task history explain intent. Compact files under `docs/agent/` route to
  those authorities; they do not replace them.
- Progressive discovery is not a limit on justified investigation. Cross-component or high-risk
  changes must still load every relevant authoritative document, implementation, and test.

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

## Architecture invariants

1. **Only** `src/telegram_media_bot/infrastructure/ytdlp/` may import `yt_dlp`.
2. Raw dictionaries returned by `yt-dlp` must never cross the adapter boundary.
3. Application and Telegram layers use only project-owned models from `domain/`.
4. Telegram handlers must not perform blocking downloads or call `yt-dlp` directly.
5. Download work runs in the worker process, not in the bot polling process.
6. Site detection is delegated to the engine. Never create a chain of domain-name `if/elif`
   statements in Telegram handlers.
7. User-facing choices are semantic (`best`, `720p`, `audio_mp3`), never raw `format_id` values.
8. All runtime secrets and operator settings belong in local `config.yaml`; no secret may be
   committed, logged, embedded in Docker images, or duplicated in source code.
9. Dependency versions are reproducible through `uv.lock`. Generate and commit it before release; production must use `uv sync --frozen`.
10. Custom extractors must be implemented as external `yt-dlp` plugins, never by modifying or
    vendoring the `yt-dlp` source tree.
11. Temporary files must live under a unique job directory and must be cleaned on success,
    failure, and cancellation according to configuration.
12. Logs must not contain bot tokens, cookies, authorization headers, full proxy credentials,
    or arbitrary user-supplied file paths.
13. All text files are UTF-8. Python file I/O must specify `encoding="utf-8"` when applicable.
14. The project must remain runnable with `./manage.sh up` after `config.yaml` has been created.
15. Do not silently weaken tests, type checking, or lint rules to make a change pass.
16. Use Python 3.14 or newer. Do not downgrade the project to an older Python generation. Preview, beta, and release-candidate interpreters require an explicit ADR and passing compatibility gates before production use.
17. Only `src/telegram_media_bot/infrastructure/telegram/mtproto/` may import `telethon`; the
    Premium user session is opt-in and may only upload to the configured staging channel.

## Layer ownership

- `domain/`: stable entities, value objects, enums, and project exceptions. No framework imports.
- `application/`: use cases and ports. May depend on `domain/`, never infrastructure details.
- `infrastructure/ytdlp/`: all direct `yt-dlp` interaction and mapping.
- `infrastructure/queue/`: ARQ/Redis implementation of queue ports.
- `telegram/`: aiogram presentation and delivery adapters.
- `workers/`: composition root for background jobs.
- `bootstrap/`: configuration, logging, and dependency construction.

## Implementation requirements

- Prefer explicit, typed models over unstructured dictionaries.
- Use `pathlib.Path`, not ad-hoc string path concatenation.
- Use `asyncio.to_thread()` for blocking engine calls from async code unless a dedicated process
  strategy is implemented and documented.
- Validate URLs, file-size limits, enabled sources, playlist policy, and duration limits before
  uploading where the required metadata exists.
- Keep error translation centralized in `infrastructure/ytdlp/error_mapper.py`.
- Keep semantic format mapping centralized in `infrastructure/ytdlp/options.py`.
- Preserve exception chaining with `raise ... from exc`.
- Never expose internal exception text directly to Telegram users.
- Ensure cancellation and worker shutdown do not leave `.part`, `.ytdl`, or temporary files.
- Make operations idempotent where possible. A queue retry must not create uncontrolled duplicate
  uploads.

## Testing gates

Before declaring a task complete, run:

```bash
uv lock --check
uv sync --frozen --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run python scripts/check_agent_context.py
uv run pytest -m "not contract" --cov=telegram_media_bot --cov-report=term-missing
```

When changing the `yt-dlp` adapter or updating `yt-dlp`, also run explicitly enabled contract
smoke tests with safe public fixtures:

```bash
RUN_CONTRACT_TESTS=1 uv run pytest -m contract
```

Contract tests must assert the project-owned contract, not every field of upstream metadata.
Network-dependent tests are not allowed in the default test suite.

## Security requirements

- Treat every URL, title, uploader name, and metadata field as untrusted.
- Do not permit user-controlled output templates, postprocessor commands, `exec`, external
  downloader commands, arbitrary headers, or arbitrary filesystem destinations.
- Resolve output paths beneath the configured job root and verify they do not escape it.
- Do not follow a user-provided local file URL. Only `http` and `https` are accepted initially.
- Keep cookies mounted read-only where practical.
- Redact credentials in logs.
- Do not add DRM circumvention.
- Respect platform terms, copyright, and operator-configured source policies.

## Configuration rules

- `config.example.yaml` is the documented source of available runtime options.
- `config.yaml` is local-only and ignored by Git.
- Configuration models use `extra="forbid"`; unknown settings must fail fast.
- Adding a setting requires updating the model, example file, configuration tests, and relevant
  documentation in the same change.
- Environment variables may select the config file path only; secrets remain in the YAML file.

## Dependency and yt-dlp update policy

- Do not run an untested self-update at application startup.
- Update with `./manage.sh upgrade-ytdlp`, review the lockfile diff, run all gates, then rebuild.
- If an update breaks the adapter, modify only the adapter/mappers unless a genuine project
  contract change has been approved and documented.
- Record accepted breaking changes as an ADR in `docs/DECISIONS.md`.
- Rollback is performed by reverting `uv.lock`/the update commit and rebuilding the image.

## Documentation and task completion

Each completed task must include:

- code;
- unit tests;
- relevant integration tests;
- documentation updates;
- a concise entry in `docs/STATUS.md`;
- no tracked secrets or generated runtime data;
- all gates passing.

Do not mark a feature complete when it only has placeholder methods, TODO-only tests, or a happy
path without failure handling.
