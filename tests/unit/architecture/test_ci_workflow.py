"""T033 static tests for the tiered CI workflow YAML invariants.

These tests validate repository structure without executing GitHub Actions. They focus on
branch-protection safety and fail-conservative behavior, not whitespace.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CI = Path(".github/workflows/ci.yml")
PUBLISH = Path(".github/workflows/publish-container.yml")

AnyWorkflow = dict[str, Any]
_HEAVY = (
    "dependency",
    "package",
    "plugin-sdk",
    "docker-runtime",
    "updater-integration",
    "installer-linux",
    "installer-windows",
)


def _ci() -> AnyWorkflow:
    text = CI.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    return data


def _on(data: AnyWorkflow) -> AnyWorkflow:
    # PyYAML reads the YAML 1.1 boolean `on` as True; GitHub Actions treats it as a key.
    raw: Any = data
    triggers: Any = raw.get("on") or raw.get(True) or raw.get("true")
    assert isinstance(triggers, dict)
    return triggers


def test_ci_has_no_workflow_level_path_filters() -> None:
    triggers = _on(_ci())
    # Empty `pull_request:` / `push:` definitions parse to None under YAML, which GitHub treats as
    # "all PRs / all pushes". Either form is acceptable as long as no `paths:` filter hides checks.
    for trigger in ("pull_request", "push"):
        spec = triggers.get(trigger)
        if isinstance(spec, dict):
            assert "paths" not in spec, trigger  # required checks must never disappear
    assert "workflow_dispatch" in triggers
    assert "schedule" in triggers


def test_ci_concurrency_cancels_development_runs_safely() -> None:
    data = _ci()
    concurrency = data["concurrency"]
    assert "ci-${{ github.workflow }}-${{ github.ref }}" in concurrency["group"]
    assert concurrency["cancel-in-progress"] is True


def test_ci_heavy_jobs_depend_on_change_detection_and_are_conditional() -> None:
    data = _ci()
    jobs = data["jobs"]
    assert "change-detection" in jobs
    for name in _HEAVY:
        job = jobs[name]
        assert job["needs"] == ["change-detection"], name
        condition = str(job.get("if", ""))
        assert condition.startswith("always() &&"), name
        assert "needs.change-detection.outputs" in condition, name
        # No safety-critical heavy job may silently continue on error.
        assert not any(step.get("continue-on-error") for step in job.get("steps", [])), name


def test_change_detection_supports_manual_full_mode() -> None:
    triggers = _on(_ci())
    dispatch = triggers["workflow_dispatch"]
    assert isinstance(dispatch, dict)
    assert "full_validation" in dispatch.get("inputs", {})


def test_quality_job_is_stable_required_compatible_name() -> None:
    # Branch protection may already require `CI / quality`; the fast lane keeps that exact job name.
    jobs = _ci()["jobs"]
    assert "quality" in jobs
    assert jobs["quality"]["if"] == "always()"


def test_final_gate_is_always_evaluating_and_understands_cancelled() -> None:
    data = _ci()
    gate = data["jobs"]["final-ci-gate"]
    assert gate["if"] == "always()"
    needs = gate["needs"]
    assert "change-detection" in needs
    assert "quality" in needs
    for name in _HEAVY:
        assert name in needs
    env_keys: dict[str, Any] = {}
    for step in gate["steps"]:
        if isinstance(step.get("env"), dict):
            env_keys.update(step["env"])
    for job in ("DKR", "UPD", "ILX", "IWX", "DEP", "PKG", "PLG"):
        assert job in env_keys
    assert all("needs." in str(v) for v in env_keys.values())
    # The gate inspects job.result (success/failure/cancelled/skipped) structurally.
    assert any("was cancelled" in str(step) for step in gate["steps"])
    assert any("skipped" in str(step) for step in gate["steps"])


def test_final_gate_is_the_merge_blocking_aggregate_check() -> None:
    """final-ci-gate is the stable aggregate check branch protection must require.

    It depends on change-detection, the quality fast lane, and every conditional heavy lane, and
    it cannot pass when a required heavy lane failed/cancelled or classification itself failed.
    Requiring only `quality` + `change-detection` would let a relevant heavy-lane failure slip.
    """
    data = _ci()
    gate = data["jobs"]["final-ci-gate"]
    needs = list(gate["needs"])
    assert "change-detection" in needs
    assert "quality" in needs
    for name in _HEAVY:
        assert name in needs, name

    gate_body = " ".join(str(step) for step in gate["steps"])
    # A required heavy job failing, cancelling, or being wrongly skipped must fail the gate.
    assert "was cancelled" in gate_body
    assert "failed" in gate_body
    assert "was required but skipped" in gate_body
    # change-detection failure must fail the gate (conservative: reject the run, never pass).
    assert "CHANGE_DETECTION" in gate_body and '"success"' in gate_body

    # No gate step preserves upstream failure via continue-on-error.
    assert not any(step.get("continue-on-error") for step in gate["steps"])


def test_docs_name_final_ci_gate_as_required_branch_protection_check() -> None:
    decisions = Path("docs/DECISIONS.md").read_text(encoding="utf-8")
    status = Path("docs/STATUS.md").read_text(encoding="utf-8")
    task = Path("docs/tasks/T033-fast-feedback-ci.md").read_text(encoding="utf-8")
    code_map = Path("docs/CODE_MAP.md").read_text(encoding="utf-8")
    for doc in (decisions, status, task, code_map):
        assert "final-ci-gate" in doc
    # Branch-protection guidance must name final-ci-gate as the merge-blocking required check and
    # must explicitly reject `quality` + `change-detection` alone as insufficient.
    combined = decisions + status + task + code_map
    assert "merge-blocking" in combined or "merge-safety" in combined
    assert "quality" in combined and "change-detection" in combined


def test_docker_compose_restart_policy_validation_remains_in_ci() -> None:
    data = _ci()
    runs = " ".join(
        step.get("run", "") for job in ("docker-runtime",) for step in data["jobs"][job]["steps"]
    )
    assert "docker compose --profile local-api config" in runs
    assert "unless-stopped" in runs


def test_updater_integration_retains_local_api_readiness_scenario() -> None:
    data = _ci()
    runs = " ".join(step.get("run", "") for step in data["jobs"]["updater-integration"]["steps"])
    assert "test_local_api_readiness.sh" in runs


def test_updater_integration_builds_and_loads_the_runtime_image() -> None:
    """The updater matrix needs the locally-loaded `telegram-media-downloader-bot:ci` image.

    Before T033 this came from the combined `docker` job; after the split the updater lane runs as an
    independent job (it can be required while `docker` is false), so it must build+load its own tag
    from the shared GHA cache before executing the matrix. Assert this so a fresh-runner "No such
    image" regression stays impossible.
    """
    data = _ci()
    steps = data["jobs"]["updater-integration"]["steps"]
    build_steps = [step for step in steps if step.get("uses", "") == "docker/build-push-action@v6"]
    assert build_steps, "updater-integration must build/load telegram-media-downloader-bot:ci"
    with_ = build_steps[0]["with"]
    assert with_["load"] is True
    assert "telegram-media-downloader-bot:ci" in with_.get("tags", "")
    assert "telegram-media-downloader-bot-amd64" in with_.get("cache-from", "")


def test_no_workflow_publishes_on_push_except_release_tags() -> None:
    data = yaml.safe_load(PUBLISH.read_text(encoding="utf-8"))
    triggers = data.get("on") or data.get(True)
    assert triggers["push"]["tags"] == ["v*"]
    assert "workflow_dispatch" in triggers
    assert data["permissions"] == {}
    assert data["jobs"]["publish"]["if"] == (
        "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
    )
    assert data["jobs"]["release"]["needs"] == "publish"


def test_ci_permissions_are_least_privilege() -> None:
    assert _ci()["permissions"] == {"contents": "read"}


def test_quality_scripts_reside_in_repo_and_collect_together() -> None:
    for path in (
        "scripts/ci_fast_quality.sh",
        "scripts/ci_docs_quality.sh",
        "scripts/ci_change_policy.py",
    ):
        assert Path(path).is_file(), path
    assert (
        Path("scripts/ci_fast_quality.sh")
        .read_text(encoding="utf-8")
        .count('uv run pytest -m "not contract"')
        >= 1
    )
    assert "check_agent_context.py" in Path("scripts/ci_docs_quality.sh").read_text(
        encoding="utf-8"
    )
