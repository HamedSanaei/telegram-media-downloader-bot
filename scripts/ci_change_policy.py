#!/usr/bin/env python3
"""Deterministic change classifier for the tiered CI workflow (T033).

The classifier is pure and unit-testable without GitHub Actions. It maps a set of changed paths
(plus a small amount of event context) onto validation lanes. It never relies on workflow-level
`paths:` filters; the CI workflow always starts, reports the stable ``change-detection``,
``fast-quality``, and ``final-ci-gate`` checks, and only *this* route decides whether heavy lanes
are needed.

Failure is conservative. A path that cannot be recognised, a root/unknown file, a workflow file
change, an unclear event shape, or missing change history all force broad validation rather than
skipping heavy work.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

# --------------------------------------------------------------------------- #
# Lane keys (kept stable so workflows and branch protection depend on them)
# --------------------------------------------------------------------------- #
QUALITY = "quality"
DOCS_ONLY = "docs_only"
DEPENDENCY = "dependency"
PACKAGE = "package"
PLUGIN_SDK = "plugin_sdk"
DOCKER = "docker"
UPDATER = "updater"
INSTALLER_LINUX = "installer_linux"
INSTALLER_WINDOWS = "installer_windows"
CONSERVATIVE = "conservative"

#: Every lane that the workflow renders as a job name / output flag.
LANES = (
    DOCS_ONLY,
    DEPENDENCY,
    PACKAGE,
    PLUGIN_SDK,
    DOCKER,
    UPDATER,
    INSTALLER_LINUX,
    INSTALLER_WINDOWS,
    CONSERVATIVE,
)


@dataclass(frozen=True)
class CiChangePolicy:
    """The validation-lane decision for one CI run."""

    docs_only: bool = False
    dependency: bool = False
    package: bool = False
    plugin_sdk: bool = False
    docker: bool = False
    updater: bool = False
    installer_linux: bool = False
    installer_windows: bool = False
    conservative: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            DOCS_ONLY: self.docs_only,
            DEPENDENCY: self.dependency,
            PACKAGE: self.package,
            PLUGIN_SDK: self.plugin_sdk,
            DOCKER: self.docker,
            UPDATER: self.updater,
            INSTALLER_LINUX: self.installer_linux,
            INSTALLER_WINDOWS: self.installer_windows,
            CONSERVATIVE: self.conservative,
        }


# --------------------------------------------------------------------------- #
# Path category rules
# --------------------------------------------------------------------------- #
PY = "python"
TESTS = "tests"
DOCS = "docs"
DEPS = "deps"
PACKAGE_CAT = "package"
PLUGIN = "plugin"
DOCKER_CAT = "docker"
UPGRADE = "upgrade"
LINUX = "linux"
WINDOWS = "windows"
WORKFLOW = "workflow"
POLICY = "policy"
UNKNOWN = "unknown"

_ROOT_DOC_FILES = (
    "README.md",
    "README",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "AGENTS.md",
)
_DOC_DIRS = ("docs/",)

_DEPENDENCY_FILES = ("pyproject.toml", "uv.lock", "requirements.txt", ".python-version")
_PACKAGE_FILES = (
    "scripts/check_package_assets.py",
    "scripts/check_release_policy.py",
    "release-policy.json",
    "pyproject.toml",
    "uv.lock",
)
_WORKFLOW_DIRS = (".github/workflows/", ".github/actions/")
_WORKFLOW_TOP_FILES = (
    ".github/workflows/ci.yml",
    ".github/workflows/publish-container.yml",
    ".github/dependabot.yml",
    "renovate.json",
)
_DOCKER_TOP_FILES = (
    "Dockerfile",
    ".dockerignore",
    "docker-compose.yml",
    "docker-compose.yaml",
    "config.example.yaml",
)
_LINUX_FILES = (
    "install.sh",
    "manage.sh",
    "scripts/tmb.sh",
    "scripts/build_release_archives.sh",
    "scripts/tests/test_tmb_update.sh",
    "scripts/tests/test_tmb_upgrade_integration.sh",
    "scripts/tests/test_local_api_readiness.sh",
    "scripts/tests/test_readonly_logger_preflight.sh",
)
_WINDOWS_FILES = (
    "install.ps1",
    "manage.ps1",
    "scripts/tmb.ps1",
    "scripts/tests/Test-TmbUpdate.ps1",
)
_UPGRADE_FILES = (
    "install.sh",
    "install.ps1",
    "manage.sh",
    "manage.ps1",
    "scripts/tmb.sh",
    "scripts/tmb.ps1",
    "scripts/build_release_archives.sh",
    "scripts/check_release_policy.py",
    "scripts/tests/test_tmb_update.sh",
    "scripts/tests/test_tmb_upgrade_integration.sh",
    "scripts/tests/test_local_api_readiness.sh",
    "scripts/tests/test_readonly_logger_preflight.sh",
    "scripts/tests/Test-TmbUpdate.ps1",
    "release-policy.json",
    "docker-compose.yml",
    "docker-compose.yaml",
    "Dockerfile",
    ".dockerignore",
)

#: Source paths that materially affect the runtime Docker image. Ordinary domain/application
#: edits intentionally avoid this list so PRs get fast feedback; main pushes gate them separately.
_DOCKER_SRC_DIRS = (
    "src/telegram_media_bot/infrastructure/",
    "src/telegram_media_bot/telegram/",
    "src/telegram_media_bot/workers/",
    "src/telegram_media_bot/bootstrap/",
    "src/telegram_media_bot/assets/",
    "plugins/example_extractor/",
)
_DOCKER_SRC_FILES = (
    "src/telegram_media_bot/cli.py",
    "src/telegram_media_bot/__init__.py",
    "src/telegram_media_bot/__main__.py",
)

#: Source paths that affect persistent-path, filesystem, Local Bot API lifecycle, or updater
#: semantics (filesystem/service/persistence/backup/restore/ownership contracts).
_UPDATER_SRC_DIRS = (
    "src/telegram_media_bot/infrastructure/persistence/",
    "src/telegram_media_bot/infrastructure/storage/",
    "src/telegram_media_bot/infrastructure/telegram/",
    "src/telegram_media_bot/infrastructure/security/",
)
_UPDATER_SRC_FILES = (
    "src/telegram_media_bot/cli.py",
    "src/telegram_media_bot/bootstrap/config.py",
)

_PLUGIN_DIRS = ("plugins/example_extractor/",)


def _classify_one(path: str) -> frozenset[str]:
    """Return the category set for a single changed path (never ``empty``)."""
    normalized = PurePosixPath(path)
    name = normalized.name
    first = normalized.parts[0] if normalized.parts else ""

    categories: set[str] = set()

    if first == "docs" or name in _ROOT_DOC_FILES or name.casefold().endswith(".md"):
        categories.add(DOCS)
    elif first == ".github":
        if len(normalized.parts) >= 2 and normalized.parts[1] in ("workflows", "actions"):
            categories.add(WORKFLOW)
        else:
            categories.add(WORKFLOW)
            categories.add(UNKNOWN)  # unknown workflow/config area is conservative

    if first == "src":
        categories.add(PY)
        if any(str(normalized).startswith(prefix) for prefix in _DOCKER_SRC_DIRS):
            categories.add(DOCKER_CAT)
        if str(normalized) in _DOCKER_SRC_FILES:
            categories.add(DOCKER_CAT)
        if any(str(normalized).startswith(prefix) for prefix in _UPDATER_SRC_DIRS):
            categories.add(UPGRADE)
        if str(normalized) in _UPDATER_SRC_FILES:
            categories.add(UPGRADE)
    elif first == "tests":
        categories.add(PY)
        categories.add(TESTS)
    elif first == "plugins" and str(normalized).startswith(_PLUGIN_DIRS[0]):
        categories.add(PLUGIN)
        categories.add(DOCKER_CAT)
        if str(normalized) in (
            "plugins/example_extractor/pyproject.toml",
            "plugins/example_extractor/uv.lock",
        ):
            categories.add(DEPS)
            categories.add(PACKAGE_CAT)

    if first == "scripts":
        if name.endswith(".py"):
            categories.add(PY)
        if path in _UPGRADE_FILES:
            categories.add(UPGRADE)
        if path in _PACKAGE_FILES:
            categories.add(PACKAGE_CAT)

    if name.endswith(".sh"):
        categories.add(LINUX)
        if path in _UPGRADE_FILES:
            categories.add(UPGRADE)
    if name.endswith(".ps1"):
        categories.add(WINDOWS)
        if path in _UPGRADE_FILES:
            categories.add(UPGRADE)

    if str(normalized) in _DEPENDENCY_FILES:
        categories.add(DEPS)
        categories.add(PACKAGE_CAT)
    if str(normalized) in _DOCKER_TOP_FILES:
        categories.add(DOCKER_CAT)
    if str(normalized) in _UPGRADE_FILES:
        categories.add(UPGRADE)
    if path == "scripts/check_release_policy.py":
        categories.add(POLICY)
    if str(normalized) in _WORKFLOW_TOP_FILES:
        categories.add(WORKFLOW)

    if str(normalized) == "release-policy.json":
        categories.add(POLICY)
        categories.add(UPGRADE)
        categories.add(PACKAGE_CAT)

    # Canonical permitted toolbox/config/root files.
    _harmless = {
        "config.example.yaml",
        "install.sh",
        "install.ps1",
        "manage.sh",
        "manage.ps1",
        "Dockerfile",
        ".dockerignore",
        "docker-compose.yml",
        "docker-compose.yaml",
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        ".python-version",
        "release-policy.json",
        ".gitignore",
        ".pre-commit-config.yaml",
        ".tool-versions",
        "renovate.json",
        "Makefile",
        "AGENTS.md",
    }
    if (
        first != "src"
        and first != "tests"
        and first != "docs"
        and first != "plugins"
        # Non-release-tooling unknown areas (e.g. new top-level directories) are conservative.
        and str(normalized) not in _harmless
        and not categories
    ):
        categories.add(UNKNOWN)

    if not categories:
        categories.add(UNKNOWN)
    return frozenset(categories)


def classify_paths(
    changed_paths: Iterable[str],
    *,
    main_push: bool = False,
) -> CiChangePolicy:
    """Classify a set of changed paths into validation lanes.

    ``main_push`` makes ordinary source/tests changes trigger Docker runtime validation so main can
    never accumulate a broken image even when the underlying reason was a generic Python edit.
    """
    paths = [str(p) for p in changed_paths if str(p).strip()]
    if not paths:
        return CiChangePolicy(docs_only=True)

    any_source = False
    any_docs = False
    dependency = False
    package = False
    plugin = False
    docker = False
    updater = False
    installer_linux = False
    installer_windows = False
    workflow = False
    policy = False
    unknown = False

    for path in paths:
        cats = _classify_one(path)
        if PY in cats or TESTS in cats:
            any_source = True
        if DOCS in cats:
            any_docs = True
        if DEPS in cats:
            dependency = True
        if PACKAGE_CAT in cats:
            package = True
        if PLUGIN in cats:
            plugin = True
        if DOCKER_CAT in cats:
            docker = True
        if UPGRADE in cats:
            updater = True
        if LINUX in cats:
            installer_linux = True
        if WINDOWS in cats:
            installer_windows = True
        if WORKFLOW in cats:
            workflow = True
        if POLICY in cats:
            policy = True
        if UNKNOWN in cats:
            unknown = True

    if dependency or package or plugin:
        dependency = True
    if plugin:
        package = True

    # Conservative fallback: unknown/root-critical file, workflow config change, or policy change
    # all expand validation; they can never classify as "cheap".
    conservative = unknown or workflow

    # Release policy changes drive update-policy safety (installers embed the withdrawn-release
    # policy), so expand to updater + both installer lanes without requiring broad full mode.
    if policy:
        updater = True
        installer_linux = True
        installer_windows = True

    # Linux/Windows installer/updater scripts also imply the updater lane when they are updater or
    # installer tests (already handled by UPGRADE category for those exact files).
    if any_source and main_push:
        docker = True

    docs_only = bool(
        any_docs
        and not any_source
        and not dependency
        and not package
        and not docker
        and not updater
        and not installer_linux
        and not installer_windows
        and not unknown
        and not workflow
        and not policy
    )

    policy_result = CiChangePolicy(
        docs_only=docs_only,
        dependency=dependency,
        package=package,
        plugin_sdk=plugin,
        docker=docker,
        updater=updater,
        installer_linux=installer_linux,
        installer_windows=installer_windows,
        conservative=conservative,
    )
    if policy_result.conservative:
        # Fail conservative: an unrecognised/unsafe classification must request every heavy lane,
        # never default to skipping heavy work.
        policy_result = CiChangePolicy(
            dependency=True,
            package=True,
            plugin_sdk=True,
            docker=True,
            updater=True,
            installer_linux=True,
            installer_windows=True,
            conservative=True,
        )
    return policy_result


def classify_event(
    event_name: str,
    changed_paths: Iterable[str],
    *,
    full: bool = False,
    main_push: bool = False,
) -> CiChangePolicy:
    """Classify for a GitHub event.

    ``full`` forces every heavy lane (dispatch/schedule/full mode). ``main_push`` (a push to main)
    makes ordinary source/tests changes trigger Docker runtime validation so main can never
    accumulate a broken image even when the underlying edit looked like a generic Python change.
    """
    policy = classify_paths(changed_paths, main_push=main_push)
    if full:
        return CiChangePolicy(
            docs_only=False,
            dependency=True,
            package=True,
            plugin_sdk=True,
            docker=True,
            updater=True,
            installer_linux=True,
            installer_windows=True,
            conservative=False,
        )
    return policy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths-file", help="newline-separated changed path list")
    parser.add_argument("--main-push", action="store_true", help="event is a push to main")
    parser.add_argument("--full", action="store_true", help="force full validation")
    parser.add_argument("--json", action="store_true", help="emit lane decisions as JSON")
    args = parser.parse_args(argv)

    if args.paths_file:
        with open(args.paths_file, encoding="utf-8") as handle:
            paths = [line.strip() for line in handle if line.strip()]
    else:
        paths = []
    policy = classify_event(
        "push" if args.main_push else "pull_request",
        paths,
        full=args.full,
        main_push=args.main_push,
    )

    if args.json:
        print(json.dumps({"main_push": args.main_push, **policy.as_dict()}, sort_keys=True))
        return 0
    # Default: emit `LANE=0|1` lines for the workflow to splice into GITHUB_OUTPUT.
    for lane in LANES:
        value = getattr(policy, lane)
        print(f"{lane}={int(value)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
