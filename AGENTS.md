# AGENTS.md

## Mission

Build and maintain a reliable Telegram media downloader bot powered by `yt-dlp` while keeping
all application code insulated from `yt-dlp` internals. The project must remain easy to update,
test, deploy, roll back, and hand over to another engineer or coding agent.

## Mandatory first steps for every task

1. Read this file completely.
2. Read `docs/PROJECT_SPEC.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`,
   `docs/CODE_MAP.md`, `docs/STATUS.md`, and the relevant task file under `docs/tasks/`.
3. Inspect existing code and tests before proposing changes.
4. Preserve the architecture invariants below.
5. Update `docs/STATUS.md` and `docs/CODE_MAP.md` whenever behavior or file ownership changes.

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
