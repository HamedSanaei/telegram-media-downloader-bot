# Codex execution guide

## Recommended workflow

1. Create a single feature branch, for example `feat/complete-media-bot-v1`.
2. Give Codex the content of `PROMPT_FOR_CODEX.md`.
3. Tell Codex to work through `docs/tasks/` in numeric order without intermediate confirmation.
4. Require one final implementation report containing exact test counts and coverage.
5. Review architectural hot spots before merging:
   - any `yt_dlp` import outside the adapter;
   - raw dictionaries crossing layers;
   - secrets or runtime data tracked by Git;
   - blocking work in aiogram handlers;
   - user-controlled output paths or commands.

## Suggested final command set

```bash
uv lock --check
uv sync --frozen --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
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

## Shared Docker build cache

The CI runtime-image build and the tag-only publication build both use the GitHub Actions BuildKit
cache scope `telegram-media-downloader-bot-amd64`. The first build on an empty or evicted cache may
still take a long time because it compiles the pinned official Telegram Local Bot API from source.
Later CI and release builds should import the compiled `telegram-bot-api-build` stage from the
shared GHA cache.

The Telegram stage intentionally has no `COPY` or `ADD` from the application repository and does
not consume `PYTHON_VERSION`. Changes under `src`, `tests`, `docs`, configuration files, application
metadata, or the application Python version therefore invalidate runtime layers without rebuilding
Telegram Local Bot API. Changing `TELEGRAM_BOT_API_REF`, the Telegram stage commands/toolchain, or
its Debian base image intentionally causes a full Telegram API rebuild.

In GitHub Actions, expand the `docker/build-push-action` step and inspect the BuildKit output. A
successful restore shows the GHA cache import followed by `CACHED` entries for the
`telegram-bot-api-build` package-install and source-build steps. The cache export at the end should
also name the same scope.
