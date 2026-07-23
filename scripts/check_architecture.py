#!/usr/bin/env python3
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "telegram_media_bot"
ALLOWED_YTDLP_ROOT = SRC / "infrastructure" / "ytdlp"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def main() -> int:
    failures: list[str] = []
    for path in SRC.rglob("*.py"):
        modules = imported_modules(path)
        imports_ytdlp = any(name == "yt_dlp" or name.startswith("yt_dlp.") for name in modules)
        if imports_ytdlp and not path.is_relative_to(ALLOWED_YTDLP_ROOT):
            failures.append(f"Direct yt_dlp import outside adapter: {path.relative_to(ROOT)}")

    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("Architecture boundary check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
