## Summary

## Architecture impact

- [ ] No `yt_dlp` import exists outside `infrastructure/ytdlp`.
- [ ] No raw upstream metadata crosses the adapter boundary.
- [ ] No secret or runtime file is tracked.
- [ ] Documentation and status files are updated.

## Verification

- [ ] `uv lock --check`
- [ ] `uv run python scripts/check_architecture.py`
- [ ] `uv run python scripts/check_text_integrity.py`
- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy src tests`
- [ ] `uv run pytest -m "not contract" --cov=telegram_media_bot`
- [ ] `uv run pre-commit run detect-secrets --all-files`
- [ ] `uv run pip-audit --local --skip-editable --progress-spinner off`
- [ ] `uv run pip check`
- [ ] `uv build`
- [ ] Docker image build
