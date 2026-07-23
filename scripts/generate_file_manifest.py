#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "FILE_MANIFEST.txt"


def _render_manifest() -> str:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    relative_paths = sorted(
        {
            Path(raw.decode("utf-8"))
            for raw in completed.stdout.split(b"\0")
            if raw and raw.decode("utf-8") != OUTPUT.name
        },
        key=lambda path: path.as_posix(),
    )
    text_attributes = _text_attributes(relative_paths)
    lines = [
        "# SHA-256 manifest for tracked and non-ignored release source files.",
        "# FILE_MANIFEST.txt excludes itself to keep generation deterministic.",
    ]
    for relative_path in relative_paths:
        path = ROOT / relative_path
        if path.is_file():
            content = path.read_bytes()
            if text_attributes.get(relative_path.as_posix()) != "unset" and b"\0" not in content:
                content = content.replace(b"\r\n", b"\n")
            digest = hashlib.sha256(content).hexdigest()
            lines.append(f"{digest}  {relative_path.as_posix()}")
    return "\n".join(lines) + "\n"


def _text_attributes(relative_paths: list[Path]) -> dict[str, str]:
    completed = subprocess.run(
        ["git", "check-attr", "-z", "text", "--", *(path.as_posix() for path in relative_paths)],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    fields = completed.stdout.decode("utf-8").split("\0")
    return {fields[index]: fields[index + 2] for index in range(0, len(fields) - 2, 3)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or verify FILE_MANIFEST.txt")
    parser.add_argument("--check", action="store_true", help="fail when the manifest is stale")
    args = parser.parse_args()
    content = _render_manifest()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != content:
            print(f"{OUTPUT} is stale; regenerate it before release")
            return 1
        print(f"Verified {OUTPUT}")
        return 0
    OUTPUT.write_text(content, encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT} with {len(content.splitlines()) - 2} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
