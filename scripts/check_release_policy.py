"""Fail closed when release tooling targets a withdrawn application version."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

POLICY_PATH = Path(__file__).resolve().parents[1] / "release-policy.json"


def normalize_release_version(value: str) -> str:
    return value[1:] if value.startswith("v") else value


def load_release_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    policy = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise ValueError("release policy root must be an object")
    return policy


def blocked_release(version: str, policy: dict[str, Any]) -> dict[str, str] | None:
    blocked = policy.get("blocked_releases")
    if not isinstance(blocked, dict):
        raise ValueError("release policy must define a blocked_releases object")
    entry = blocked.get(normalize_release_version(version))
    if entry is None:
        return None
    if not isinstance(entry, dict) or not isinstance(entry.get("reason"), str):
        raise ValueError(f"release policy entry for {version!r} is invalid")
    return entry


def assert_release_allowed(version: str, policy: dict[str, Any]) -> None:
    entry = blocked_release(version, policy)
    if entry is None:
        return
    normalized = normalize_release_version(version)
    replacement = entry.get("replacement")
    guidance = f" Use v{replacement} or newer instead." if replacement else ""
    raise ValueError(f"Release {normalized} is blocked: {entry['reason']}{guidance}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True, help="release version or v-prefixed tag")
    arguments = parser.parse_args()
    try:
        assert_release_allowed(arguments.version, load_release_policy())
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.exit(1, f"release policy check failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
