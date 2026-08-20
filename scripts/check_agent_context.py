#!/usr/bin/env python3
"""Fail fast when progressive repository-navigation safeguards or the Graphify workflow
contract regress."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_AGENT_DOCS = (
    "docs/agent/CONTEXT_INDEX.md",
    "docs/agent/ARCHITECTURE_SUMMARY.md",
    "docs/agent/DECISION_INDEX.md",
    "docs/agent/CURRENT_STATE.md",
    "docs/agent/GRAPHIFY.md",
)
REQUIRED_SKILLS = (
    "repo-navigation",
    "media-engine-change",
    "telegram-delivery-change",
    "worker-job-change",
    "persistence-change",
    "release-upgrade",
)
SKILL_MARKERS = {
    "repo-navigation": ("Graphify", "Source defines behavior", "relevant tests"),
    "media-engine-change": ("yt_dlp", "subprocess isolation", "ADR-027"),
    "telegram-delivery-change": ("must not block", "delivery_uncertain", "ADR-006"),
    "worker-job-change": ("cancellation", "cleanup", "ADR-017"),
    "persistence-change": ("SQLite/WAL", "non-destructive migrations", "ADR-007"),
    "release-upgrade": ("pre-downtime", "exact service-state", "full rollback"),
}
GRAPHIFY_WORKFLOW_FILES = (
    "AGENTS.md",
    "PROMPT_FOR_CODEX.md",
    ".agents/skills/repo-navigation/SKILL.md",
)
GRAPHIFY_WORKFLOW_MARKERS = (
    "graphify --version",
    "graphify check-update",
    "graphify update",
    "graphify query",
    "--budget 1200",
    "graphify explain",
    "graphify path",
    "discovery tooling only",
    "scripts/agent_context.py",
    "Graphify usage",
    "do not dump",
    "production/CI",
)
QUALITY_COMMANDS = (
    "uv lock --check",
    "uv sync --frozen --group dev",
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run mypy src tests",
    'uv run pytest -m "not contract"',
)


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _require_contains(
    failures: list[str], relative_path: str, text: str, needles: tuple[str, ...]
) -> None:
    lowered = text.casefold()
    for needle in needles:
        if needle.casefold() not in lowered:
            failures.append(f"{relative_path} must retain: {needle}")


def main() -> int:
    failures: list[str] = []
    for relative_path in REQUIRED_AGENT_DOCS:
        if not (ROOT / relative_path).is_file():
            failures.append(f"missing agent routing document: {relative_path}")
    for skill in REQUIRED_SKILLS:
        relative_path = f".agents/skills/{skill}/SKILL.md"
        path = ROOT / relative_path
        if not path.is_file():
            failures.append(f"missing repository skill: {relative_path}")
            continue
        text = path.read_text(encoding="utf-8")
        if f"name: {skill}" not in text:
            failures.append(f"invalid skill name in {relative_path}")
        _require_contains(failures, relative_path, text, SKILL_MARKERS[skill])

    agents = _read("AGENTS.md")
    prompt = _read("PROMPT_FOR_CODEX.md")
    graphify = _read("docs/agent/GRAPHIFY.md")
    ci = _read(".github/workflows/ci.yml")

    _require_contains(
        failures,
        "AGENTS.md",
        agents,
        (
            "infrastructure/ytdlp/` may import `yt_dlp`",
            "Temporary files must live under a unique job directory",
            "Do not silently weaken tests, type checking, or lint rules",
            "All runtime secrets and operator settings belong in local `config.yaml`",
            "Graphify is discovery tooling only",
            "source code and tests",
        ),
    )
    _require_contains(failures, "AGENTS.md", agents, QUALITY_COMMANDS)
    _require_contains(
        failures,
        "docs/agent/GRAPHIFY.md",
        graphify,
        (
            "graphify query",
            "graphify update",
            ".graphifyignore",
            "source code is authoritative",
            "scripts/agent_context.py",
        ),
    )
    _require_contains(
        failures,
        "PROMPT_FOR_CODEX.md",
        prompt,
        ("relevant Agent Skill", "Graphify", "actual source", "required final quality gate"),
    )
    forbidden_prompt_phrases = (
        "read every file under `docs/`",
        "read all `docs/tasks/",
        "scan the whole repository before",
    )
    for phrase in forbidden_prompt_phrases:
        if phrase.casefold() in prompt.casefold():
            failures.append(f"PROMPT_FOR_CODEX.md regressed to unconditional loading: {phrase}")

    for relative_path in GRAPHIFY_WORKFLOW_FILES:
        _require_contains(
            failures,
            relative_path,
            _read(relative_path),
            GRAPHIFY_WORKFLOW_MARKERS,
        )

    dependencies = tomllib.loads(_read("pyproject.toml"))["project"]["dependencies"]
    if any("graphify" in str(dependency).casefold() for dependency in dependencies):
        failures.append("Graphify must not be a production runtime dependency")
    if "scripts/check_agent_context.py" not in ci:
        failures.append("CI must run scripts/check_agent_context.py")

    graphify_ignore = ROOT / ".graphifyignore"
    if not graphify_ignore.is_file():
        failures.append("missing .graphifyignore")
    else:
        ignore_text = graphify_ignore.read_text(encoding="utf-8")
        _require_contains(
            failures,
            ".graphifyignore",
            ignore_text,
            (".venv/", "graphify-out/", "data/", "dist/", "*.log"),
        )

    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(
        "Agent context guard passed "
        f"({len(REQUIRED_AGENT_DOCS)} routing docs, {len(REQUIRED_SKILLS)} skills, "
        f"{len(GRAPHIFY_WORKFLOW_FILES)} Graphify workflow contracts, "
        f"AGENTS.md={len(agents.encode('utf-8'))} bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
