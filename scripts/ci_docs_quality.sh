#!/usr/bin/env bash
# Documentation-only fast lane (T033). For a conclusively docs-only change this runs the minimum
# safe integrity set without Docker/updater/installer/package validation. It never skips the
# repository-task/document/ADR/manifest consistency contract.
set -euo pipefail

uv lock --check
uv sync --frozen --group dev
git diff --check
uv run python scripts/check_agent_context.py
uv run python scripts/check_text_integrity.py
uv run python scripts/generate_file_manifest.py --check
uv run pre-commit run detect-secrets --all-files