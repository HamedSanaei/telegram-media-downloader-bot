"""T033 tests for the deterministic CI change classifier (no GitHub Actions needed)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ci_change_policy",
    Path(__file__).parents[3] / "scripts" / "ci_change_policy.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
_MOD = ModuleType(_SPEC.name)
sys.modules[_SPEC.name] = _MOD
_SPEC.loader.exec_module(_MOD)

CiChangePolicy: Any = _MOD.CiChangePolicy
classify_event: Any = _MOD.classify_event
classify_paths: Any = _MOD.classify_paths
QUALITY: Any = _MOD.QUALITY

HEAVY_LANES = (
    "dependency",
    "package",
    "plugin_sdk",
    "docker",
    "updater",
    "installer_linux",
    "installer_windows",
)


def _lanes(policy: Any) -> dict[str, bool]:
    result = policy.as_dict()
    assert isinstance(result, dict)
    return result


# --------------------------------------------------------------------------- #
# Positive routing
# --------------------------------------------------------------------------- #


def test_src_only_change_is_fast_and_skips_heavy_on_pr() -> None:
    lanes = _lanes(classify_paths(["src/telegram_media_bot/domain/subscriptions.py"]))
    assert lanes["docs_only"] is False
    assert lanes["docker"] is False
    assert lanes["updater"] is False
    assert lanes["installer_linux"] is False
    assert lanes["installer_windows"] is False
    assert lanes["conservative"] is False


def test_tests_only_change_is_fast() -> None:
    lanes = _lanes(classify_paths(["tests/unit/application/x_test.py"]))
    assert lanes["docker"] is False
    assert lanes["updater"] is False
    assert lanes["conservative"] is False


def test_docs_only_change_runs_docs_lane_only() -> None:
    lanes = _lanes(classify_paths(["docs/ROADMAP.md", "README.md"]))
    assert lanes["docs_only"] is True
    for lane in HEAVY_LANES:
        assert lanes[lane] is False, lane
    assert lanes["conservative"] is False


def test_dockerfile_change_activates_docker_and_updater() -> None:
    lanes = _lanes(classify_paths(["Dockerfile"]))
    assert lanes["docker"] is True
    assert lanes["updater"] is True  # image contract drives updater-integration conservatively
    assert lanes["installer_linux"] is False


def test_compose_change_activates_docker_and_updater() -> None:
    p = classify_paths(["docker-compose.yml"])
    assert _lanes(p)["docker"] is True
    assert _lanes(p)["updater"] is True


def test_uv_lock_change_activates_dependency_and_package() -> None:
    lanes = _lanes(classify_paths(["uv.lock"]))
    assert lanes["dependency"] is True
    assert lanes["package"] is True
    assert lanes["docker"] is False


def test_pyproject_change_activates_dependency_and_package() -> None:
    lanes = _lanes(classify_paths(["pyproject.toml"]))
    assert lanes["dependency"] is True
    assert lanes["package"] is True


def test_bash_updater_change_activates_linux_installer_and_updater() -> None:
    lanes = _lanes(classify_paths(["scripts/tmb.sh"]))
    assert lanes["installer_linux"] is True
    assert lanes["updater"] is True
    assert lanes["installer_windows"] is False


def test_linux_updater_test_activates_linux_and_updater() -> None:
    lanes = _lanes(classify_paths(["scripts/tests/test_tmb_update.sh"]))
    assert lanes["installer_linux"] is True
    assert lanes["updater"] is True


def test_powershell_updater_change_activates_windows_and_updater() -> None:
    lanes = _lanes(classify_paths(["scripts/tmb.ps1"]))
    assert lanes["installer_windows"] is True
    assert lanes["updater"] is True
    assert lanes["installer_linux"] is False


def test_windows_updater_test_activates_windows_and_updater() -> None:
    lanes = _lanes(classify_paths(["scripts/tests/Test-TmbUpdate.ps1"]))
    assert lanes["installer_windows"] is True
    assert lanes["updater"] is True


def test_release_policy_change_expands_safety_broadly() -> None:
    lanes = _lanes(classify_paths(["release-policy.json"]))
    assert lanes["updater"] is True
    assert lanes["installer_linux"] is True
    assert lanes["installer_windows"] is True  # installers embed the withdrawn-policy snapshot


def test_workflow_change_fails_conservative() -> None:
    lanes = _lanes(classify_paths([".github/workflows/ci.yml"]))
    assert lanes["conservative"] is True
    assert lanes["docker"] is True
    assert lanes["updater"] is True
    assert lanes["installer_linux"] is True
    assert lanes["installer_windows"] is True


def test_plugin_sdk_change_activates_plugin_package_and_dependency() -> None:
    lanes = _lanes(classify_paths(["plugins/example_extractor/src/some.py"]))
    assert lanes["plugin_sdk"] is True
    assert lanes["package"] is True
    assert lanes["dependency"] is True


def test_multiple_categories_trigger_union_of_lanes() -> None:
    p = classify_paths(["src/telegram_media_bot/domain/x.py", "scripts/tmb.sh", "uv.lock"])
    lanes = _lanes(p)
    assert lanes["updater"] is True
    assert lanes["installer_linux"] is True
    assert lanes["dependency"] is True
    assert lanes["package"] is True


# --------------------------------------------------------------------------- #
# Conservative / negative routing
# --------------------------------------------------------------------------- #


def test_unknown_root_file_fails_conservative() -> None:
    lanes = _lanes(classify_paths(["unexpected-root-file.txt"]))
    assert lanes["conservative"] is True
    assert lanes["docker"] is True


def test_unknown_top_level_directory_fails_conservative() -> None:
    lanes = _lanes(classify_paths(["vendor/something.py"]))
    assert lanes["conservative"] is True


def test_empty_change_list_is_trivial() -> None:
    lanes = _lanes(classify_paths([]))
    assert lanes["docs_only"] is True
    assert not lanes["docker"]
    assert not lanes["updater"]
    assert not lanes["conservative"]


def test_main_push_source_gates_docker_runtime() -> None:
    lanes = _lanes(classify_paths(["src/telegram_media_bot/domain/x.py"], main_push=True))
    assert lanes["docker"] is True
    assert lanes["updater"] is False
    assert lanes["installer_linux"] is False


def test_event_main_push_forwards_docker_policy_to_paths() -> None:
    # Regression: the CLI/workflow `--main-push` path must behave exactly like classify_paths(main_push=True);
    # the event helper previously dropped the main_push flag so main src pushes skipped Docker.
    lanes = _lanes(classify_event("push", ["src/telegram_media_bot/domain/x.py"], main_push=True))
    assert lanes["docker"] is True
    assert lanes["updater"] is False
    assert lanes["installer_linux"] is False
    assert lanes["installer_windows"] is False


# --------------------------------------------------------------------------- #
# Event-level behavior
# --------------------------------------------------------------------------- #


def test_manual_full_mode_forces_every_heavy_lane() -> None:
    lanes = _lanes(classify_event("workflow_dispatch", ["docs/ROADMAP.md"], full=True))
    assert lanes["docs_only"] is False
    for lane in HEAVY_LANES:
        assert lanes[lane] is True, lane


def test_history_unavailable_mode_forces_full() -> None:
    # When the workflow cannot resolve a base commit it classifies full (the conservative
    # "history unavailable" path) and must never skip heavy work.
    lanes = _lanes(classify_event("push", ["docs/ROADMAP.md"], full=True))
    assert lanes["docker"] is True
    assert lanes["updater"] is True


# --------------------------------------------------------------------------- #
# Classifier-vs-workflow contract
# --------------------------------------------------------------------------- #


def test_quality_lane_is_stable_and_never_routed_off() -> None:
    assert QUALITY == "quality"
    assert classify_paths(["docs/ROADMAP.md"]).docs_only is True


def test_conservative_never_defaults_to_skip() -> None:
    for path in ("unknown.bin", ".github/unexpected/marker.yml", "mystery/file.py"):
        lanes = _lanes(classify_paths([path]))
        assert lanes["conservative"] is True, path
        for lane in HEAVY_LANES:
            assert lanes[lane] is True, (path, lane)


@pytest.mark.parametrize(
    ("paths", "kw", "expect_docs", "expect_heavy"),
    [
        (["docs/STATUS.md"], {}, True, []),
        (["Dockerfile"], {}, False, ["docker", "updater"]),
        (["uv.lock"], {}, False, ["dependency", "package"]),
        (["pyproject.toml"], {}, False, ["dependency", "package"]),
        (["scripts/tmb.sh"], {}, False, ["updater", "installer_linux"]),
        (["scripts/tmb.ps1"], {}, False, ["updater", "installer_windows"]),
        (["release-policy.json"], {}, False, ["updater"]),
        (["plugins/example_extractor/pyproject.toml"], {}, False, ["plugin_sdk", "package"]),
        (["src/telegram_media_bot/domain/x.py"], {"main_push": True}, False, ["docker"]),
        (["src/telegram_media_bot/domain/x.py"], {}, False, []),
    ],
)
def test_routing_matrix(
    paths: list[str],
    kw: dict[str, bool],
    expect_docs: bool,
    expect_heavy: list[str],
) -> None:
    lanes = _lanes(classify_paths(paths, **kw))
    assert lanes["docs_only"] is expect_docs
    for lane in expect_heavy:
        assert lanes[lane] is True, lane
    if not expect_heavy and not lanes["conservative"]:
        # Only trivially-classified cases assert that nothing heavy is on; conservative falls
        # back deliberately and is tested separately.
        for lane in HEAVY_LANES:
            assert lanes[lane] is False, lane
