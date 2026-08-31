#!/usr/bin/env bash
# Full fast-quality lane (T033). Runs the stable fast feedback path for ordinary application
# changes: lock/sync, architecture/context/text/manifest integrity, lint/format/type-check,
# secret scan, dependency consistency check, and the ordinary non-contract coverage suite.
set -euo pipefail

uv lock --check
uv sync --frozen --group dev
uv run python scripts/check_architecture.py
uv run python scripts/check_agent_context.py
uv run python scripts/check_text_integrity.py
uv run python scripts/generate_file_manifest.py --check
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pre-commit run detect-secrets --all-files
uv run pip check
# Ordinary non-contract Python tests with coverage (still ~70s in CI).
uv run pytest -m "not contract" --cov=telegram_media_bot --cov-report=term-missing --cov-report=xml