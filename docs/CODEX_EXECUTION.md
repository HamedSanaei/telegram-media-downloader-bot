# Codex execution guide

## Recommended workflow

1. Create a single feature branch, for example `feat/complete-media-bot-v1`.
2. Give Codex the content of `PROMPT_FOR_CODEX.md`.
3. Have Codex understand the task, load the relevant repository Skill, and use
   `docs/agent/CONTEXT_INDEX.md` only when subsystem ownership is unclear.
4. Query a fresh Graphify index for the smallest working set, then inspect the identified source,
   tests, and relevant detailed spec/architecture/ADR/task sections. For environments without
   Graphify, use `scripts/agent_context.py` as the deterministic fallback.
5. For a full-project completion request, work through incomplete `docs/tasks/` in numeric order
   without intermediate confirmation; ordinary localized tasks should not preload unrelated task
   history.
6. Require one final implementation report containing exact test counts and coverage.
7. Review architectural hot spots before merging:
   - any `yt_dlp` import outside the adapter;
   - raw dictionaries crossing layers;
   - secrets or runtime data tracked by Git;
   - blocking work in aiogram handlers;
   - user-controlled output paths or commands.

The authority order is explicit: Graphify = navigation; source = actual behavior; tests = expected
behavior; project specification, architecture, and ADRs = design rationale. Query output never
waives source inspection or an applicable safeguard.

## Suggested final command set

```bash
uv lock --check
uv sync --frozen --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run python scripts/check_architecture.py
uv run python scripts/check_agent_context.py
uv run python scripts/check_text_integrity.py
uv run python scripts/generate_file_manifest.py --check
uv run pre-commit run detect-secrets --all-files
uv run pip check
uv run pip-audit --local --skip-editable --progress-spinner off
uv run pytest -m "not contract" --cov=telegram_media_bot --cov-report=term-missing
uv build
(
  cd plugins/example_extractor
  uv lock --check
  uv sync --frozen --group dev
  uv run pytest -m "not contract"
)
docker build -t telegram-media-downloader-bot:review .
```

Then optionally run contract tests using operator-selected public URLs:

```bash
RUN_CONTRACT_TESTS=1 \
CONTRACT_MEDIA_URL=https://example.invalid/replace-me \
uv run pytest -m contract
```

## Immutable Telegram Bot API artifact

The normal application Dockerfile consumes the published immutable Telegram Bot API artifact
(`ghcr.io/hamedsanaei/telegram-bot-api` pinned by full sha256 digest via
`ARG TELEGRAM_BOT_API_IMAGE`) and copies `/telegram-bot-api` from it. It never clones,
submodules, or compiles Telegram Bot API/TDLib, and it installs no compiler toolchain. The binary
is built from pinned upstream source only by the manual-only dedicated workflow
(`Dockerfile.telegram-bot-api` + `.github/workflows/build-telegram-bot-api.yml`, triggered
explicitly by `workflow_dispatch` when an upgrade is intended), which publishes the digest-pinned
artifact. A cold normal application build therefore never compiles Telegram Bot API or TDLib.

The CI runtime-image build and the tag-only publication build both use the GitHub Actions BuildKit
cache scope `telegram-media-downloader-bot-amd64`. The runtime image also bundles the official
server binary, so the pull of the pinned artifact is the only Telegram-related cost; application
changes never trigger a Telegram/TDLib rebuild. In GitHub Actions, expand the
`docker/build-push-action` step and inspect the BuildKit output: a successful build shows the
artifact image pull followed by `CACHED` entries for the Python dependency layers, with no
`Building CXX object` / `cmake --build` output.
