#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".sh",
    ".ps1",
    ".txt",
}
MOJIBAKE_MARKERS = tuple(chr(codepoint) for codepoint in (195, 194, 216, 217, 65533))
SKIP_PARTS = {".git", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def main() -> int:
    failures: list[str] = []
    checked = 0
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        checked += 1
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            failures.append(f"Invalid UTF-8: {path.relative_to(ROOT)}: {exc}")
            continue
        markers = [marker for marker in MOJIBAKE_MARKERS if marker in text]
        if markers:
            failures.append(f"Possible mojibake {markers}: {path.relative_to(ROOT)}")

    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"UTF-8/text integrity check passed for {checked} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
