#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKFILE = ROOT / "uv.lock"
REPORT_ROOT = ROOT / "data" / "state" / "upgrade-reports"


def main() -> int:
    old_version = _locked_version("yt-dlp")
    _run(["uv", "lock", "--upgrade-package", "yt-dlp"])
    new_version = _locked_version("yt-dlp")
    _run(["uv", "sync", "--frozen", "--group", "dev"])
    _run(["uv", "run", "pytest", "tests/unit/infrastructure/ytdlp", "-m", "not contract"])
    contract_status = "not requested"
    if os.environ.get("RUN_CONTRACT_TESTS") == "1":
        _run(["uv", "run", "pytest", "-m", "contract"])
        contract_status = "passed"
    report = _write_report(old_version, new_version, contract_status)
    print(f"yt-dlp: {old_version} -> {new_version}")
    print(f"Upgrade report: {report}")
    print("Next: review uv.lock, run the full check suite, then deploy to canary.")
    return 0


def _locked_version(package_name: str) -> str:
    with LOCKFILE.open("rb") as lock_file:
        lock = tomllib.load(lock_file)
    for package in lock.get("package", []):
        if package.get("name") == package_name:
            return str(package.get("version") or "unknown")
    return "not-locked"


def _run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def _write_report(old_version: str, new_version: str, contract_status: str) -> Path:
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = REPORT_ROOT / f"yt-dlp-{timestamp}.md"
    report.write_text(
        "\n".join(
            (
                "# yt-dlp upgrade report",
                "",
                f"- UTC timestamp: {timestamp}",
                f"- Previous version: `{old_version}`",
                f"- Candidate version: `{new_version}`",
                "- Adapter unit tests: passed",
                f"- External contract tests: {contract_status}",
                "",
                "## Rollback",
                "",
                "Revert the reviewed `uv.lock` change and rebuild the immutable image.",
                "Do not update dependencies inside a running container.",
                "",
            )
        ),
        encoding="utf-8",
    )
    return report


if __name__ == "__main__":
    raise SystemExit(main())
