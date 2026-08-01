import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from telegram_media_bot import __version__

TELEGRAM_BOT_API_PARENT_COMMIT = (
    "adfd7f6a8e990272851777eeb3ae0def4216f161"  # pragma: allowlist secret
)
PRODUCTION_RELEASE_CONDITION = (
    "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
)
SHARED_BUILDKIT_CACHE = "telegram-media-downloader-bot-amd64"


@pytest.mark.skipif(os.name == "nt", reason="release Bash parsing runs on Linux CI")
@pytest.mark.parametrize(
    "script",
    [
        "install.sh",
        "manage.sh",
        "scripts/tmb.sh",
        "scripts/build_release_archives.sh",
        "scripts/tests/test_tmb_update.sh",
        "scripts/tests/test_tmb_upgrade_integration.sh",
    ],
)
def test_complete_release_bash_script_parses(script: str) -> None:
    subprocess.run(["bash", "-n", script], check=True)


def test_python_build_argument_is_global() -> None:
    instructions = [
        line.strip()
        for line in Path("Dockerfile").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert instructions[0] == "ARG PYTHON_VERSION=3.14.5"
    assert "FROM python:${PYTHON_VERSION}-slim AS runtime" in instructions


def test_app_containers_are_read_only_and_drop_capabilities() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    common = compose["x-app-common"]

    assert common["read_only"] is True
    assert common["cap_drop"] == ["ALL"]
    assert common["security_opt"] == ["no-new-privileges:true"]
    assert common["restart"] == "on-failure:5"
    assert any(mount.startswith("/tmp:") for mount in common["tmpfs"])
    assert compose["services"]["worker"]["cpus"] == "${TMB_WORKER_CPUS:-0}"


def test_config_path_is_explicit_and_local_api_secrets_are_not_in_container_files() -> None:
    compose_text = Path("docker-compose.yml").read_text(encoding="utf-8")
    dockerfile_text = Path("Dockerfile").read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)

    assert "environment" not in compose["x-app-common"]
    assert compose["services"]["bot"]["command"][-2:] == ["--config", "/app/config.yaml"]
    assert compose["services"]["worker"]["command"][-2:] == ["--config", "/app/config.yaml"]
    assert "APP_CONFIG_PATH" not in dockerfile_text
    for forbidden in ("api_hash", "api_id", "bot_token"):
        assert forbidden not in compose_text.casefold()
        assert forbidden not in dockerfile_text.casefold()


def test_telegram_bot_api_uses_full_parent_commit_before_syncing_submodules() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"^ARG TELEGRAM_BOT_API_REF=(?P<ref>[0-9a-f]+)$", dockerfile, re.MULTILINE)

    assert match is not None
    assert re.fullmatch(r"[0-9a-f]{40}", match["ref"])
    assert match["ref"] == TELEGRAM_BOT_API_PARENT_COMMIT
    clone = "git clone --filter=blob:none --no-checkout"
    checkout = 'git checkout --detach "${TELEGRAM_BOT_API_REF}"'
    submodules = "git submodule update --init --recursive"
    assert dockerfile.index(clone) < dockerfile.index(checkout) < dockerfile.index(submodules)
    assert "git clone --filter=blob:none --recursive" not in dockerfile


def test_telegram_bot_api_build_stage_is_isolated_from_application_changes() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    stage_start = dockerfile.index("FROM debian:bookworm-slim AS telegram-bot-api-build")
    stage_end = dockerfile.index("FROM python:${PYTHON_VERSION}-slim AS runtime")
    stage = dockerfile[stage_start:stage_end]

    assert "ARG TELEGRAM_BOT_API_REF" in stage
    assert "${PYTHON_VERSION}" not in stage
    assert not re.search(r"^(?:COPY|ADD)\s", stage, re.MULTILINE)
    for application_input in ("tests", "docs", "config.example.yaml", "pyproject.toml"):
        assert application_input not in stage
    assert "COPY --from=telegram-bot-api-build" in dockerfile[stage_end:]


def test_bot_worker_and_local_api_share_the_pinned_application_build() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    common = compose["x-app-common"]

    for service_name in ("bot", "worker", "local-api"):
        service = compose["services"][service_name]
        assert service["build"] == common["build"]
        assert service["image"] == common["image"]


def test_powershell_analysis_invokes_each_script_with_a_scalar_path() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "Invoke-ScriptAnalyzer -Path install.ps1,scripts/tmb.ps1" not in workflow
    assert 'issues = @("install.ps1", "scripts/tmb.ps1") | ForEach-Object' in workflow
    assert "Invoke-ScriptAnalyzer -Path $_ -Recurse" in workflow


def test_release_workflow_is_tag_only_and_least_privilege() -> None:
    workflow = yaml.load(
        Path(".github/workflows/publish-container.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )

    assert workflow["on"]["push"]["tags"] == ["v*"]
    assert "workflow_dispatch" in workflow["on"]
    assert workflow["permissions"] == {}
    assert workflow["jobs"]["publish"]["if"] == PRODUCTION_RELEASE_CONDITION
    assert workflow["jobs"]["release"]["if"] == PRODUCTION_RELEASE_CONDITION
    assert workflow["jobs"]["publish"]["permissions"] == {
        "contents": "read",
        "packages": "write",
    }
    assert workflow["jobs"]["release"]["permissions"] == {"contents": "write"}
    assert workflow["jobs"]["release"]["needs"] == "publish"


def test_ci_builds_and_smoke_tests_runtime_with_shared_buildkit_cache() -> None:
    workflow = yaml.load(
        Path(".github/workflows/ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["docker"]["steps"]
    setup_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("uses") == "docker/setup-buildx-action@v3"
    )
    build_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("uses") == "docker/build-push-action@v6"
    )
    build = steps[build_index]["with"]
    runs = [step.get("run", "") for step in steps]

    assert workflow["permissions"] == {"contents": "read"}
    assert setup_index < build_index
    assert build["context"] == "."
    assert build["push"] == "false"
    assert build["load"] == "true"
    assert build["pull"] == "true"
    assert build["platforms"] == "linux/amd64"
    assert build["tags"] == "telegram-media-downloader-bot:ci"
    assert "PYTHON_VERSION=3.14.5" in build["build-args"]
    assert build["cache-from"] == f"type=gha,scope={SHARED_BUILDKIT_CACHE}"
    assert build["cache-to"] == f"type=gha,mode=max,scope={SHARED_BUILDKIT_CACHE}"
    assert "docker compose --profile local-api config" in runs
    assert any(
        "docker run --rm telegram-media-downloader-bot:ci telegram-media-bot --help" in run
        for run in runs
    )
    assert any("native_selection_smoke" in run for run in runs)
    assert any("native_ui_smoke" in run for run in runs)
    assert any("command -v ffmpeg" in run for run in runs)
    assert any("command -v ffprobe" in run for run in runs)
    assert any("command -v 7zz || command -v 7z" in run for run in runs)
    assert any('"$seven_zip" t /tmp/smoke.zip.001' in run for run in runs)
    assert any("telegram-media-bot doctor --config /app/config.example.yaml" in run for run in runs)
    assert any("usage_chart_smoke" in run for run in runs)
    assert any("--verify-uid 10001" in run for run in runs)
    assert any("--network none --read-only" in run for run in runs)
    artifact = next(step for step in steps if step.get("uses") == "actions/upload-artifact@v4")
    assert artifact["with"]["name"] == "usage-chart-smoke"
    assert "usage-chart-weekly-smoke.png" in artifact["with"]["path"]
    assert "usage-chart-monthly-smoke.png" in artifact["with"]["path"]
    assert any("RUN_PRIVILEGED_UPGRADE_TESTS" in str(step.get("env", "")) for step in steps)
    assert any("test_tmb_upgrade_integration.sh" in run for run in runs)
    assert all("docker compose --profile local-api build" not in run for run in runs)


def test_release_uses_the_same_shared_buildkit_cache_scope_as_ci() -> None:
    workflow = yaml.load(
        Path(".github/workflows/publish-container.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    build_step = next(
        step
        for step in workflow["jobs"]["publish"]["steps"]
        if step.get("uses") == "docker/build-push-action@v6"
    )
    build = build_step["with"]

    assert build["cache-from"] == f"type=gha,scope={SHARED_BUILDKIT_CACHE}"
    assert build["cache-to"] == f"type=gha,mode=max,scope={SHARED_BUILDKIT_CACHE}"
    assert build["platforms"] == "linux/amd64"
    assert "PYTHON_VERSION=3.14.5" in build["build-args"]


def test_release_workflow_generates_stable_and_prerelease_tags_safely() -> None:
    workflow = Path(".github/workflows/publish-container.yml").read_text(encoding="utf-8")
    image = "ghcr.io/hamedsanaei/telegram-media-downloader-bot"

    def expected_tags(tag: str) -> list[str]:
        version = tag.removeprefix("v")
        major, minor, _patch = version.split(".", maxsplit=2)
        tags = [f"{image}:{tag}", f"{image}:{version}", f"{image}:{major}.{minor}"]
        if "-" not in version:
            tags.append(f"{image}:latest")
        return tags

    assert expected_tags("v1.0.11") == [
        f"{image}:v1.0.11",
        f"{image}:1.0.11",
        f"{image}:1.0",
        f"{image}:latest",
    ]
    assert f"{image}:latest" not in expected_tags("v1.1.0-beta.1")
    assert "type=raw,value=${{ github.ref_name }}" in workflow
    assert "type=semver,pattern={{version}},value=${{ github.ref_name }}" in workflow
    assert "type=semver,pattern={{major}}.{{minor}},value=${{ github.ref_name }}" in workflow
    assert "type=raw,value=latest,enable=${{ !contains(github.ref_name, '-') }}" in workflow
    assert "platforms: linux/amd64" in workflow
    assert "linux/arm64" not in workflow


def test_release_tag_exactly_matches_project_version() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    workflow = Path(".github/workflows/publish-container.yml").read_text(encoding="utf-8")
    version = project["project"]["version"]
    tag = f"v{version}"

    assert version == "1.0.11"
    assert __version__ == version
    assert tag == "v1.0.11"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", tag)
    assert 'if tag != f"v{version}":' in workflow


def test_release_waits_for_published_image_smoke_test_and_attaches_verified_assets() -> None:
    workflow = Path(".github/workflows/publish-container.yml").read_text(encoding="utf-8")

    assert 'if tag != f"v{version}":' in workflow
    assert "Tag {tag} does not match pyproject.toml version {version}" in workflow
    assert 'docker run --rm "$image" telegram-media-bot --help' in workflow
    assert "native_selection_smoke" in workflow
    assert "native_ui_smoke" in workflow
    assert "command -v ffmpeg" in workflow
    assert "command -v ffprobe" in workflow
    assert "command -v 7zz || command -v 7z" in workflow
    assert '"$seven_zip" t /tmp/smoke.zip.001' in workflow
    assert "telegram-media-bot doctor --config /app/config.example.yaml" in workflow
    assert "usage_chart_smoke" in workflow
    assert "--verify-uid 10001" in workflow
    assert "--network none --read-only" in workflow
    assert "test_tmb_upgrade_integration.sh" in workflow
    assert "scripts/build_release_archives.sh" in workflow
    assert "tmb-current.sh" in workflow
    assert "generate_release_notes: true" in workflow
    assert "sha256sum --check telegram-media-downloader-bot.tar.gz.sha256" in workflow
    assert "sha256sum --check telegram-media-downloader-bot.zip.sha256" in workflow
    for label in (
        "org.opencontainers.image.source",
        "org.opencontainers.image.version",
        "org.opencontainers.image.revision",
    ):
        assert label in workflow
    for asset in (
        "telegram-media-downloader-bot.tar.gz",
        "telegram-media-downloader-bot.tar.gz.sha256",
        "telegram-media-downloader-bot.zip",
        "telegram-media-downloader-bot.zip.sha256",
    ):
        assert asset in workflow


def test_management_cleanup_is_project_scoped_and_runs_after_update_verification() -> None:
    linux = Path("scripts/tmb.sh").read_text(encoding="utf-8")
    windows = Path("scripts/tmb.ps1").read_text(encoding="utf-8")
    repository = "ghcr.io/hamedsanaei/telegram-media-downloader-bot"

    for script in (linux, windows):
        assert repository in script
        assert "prune_old_project_images_after_success" in script
        assert "cleanup-workspaces" in script
        assert "docker image prune" not in script
        assert "docker system prune" not in script
        assert "docker volume prune" not in script

    assert linux.index("verify_runtime_release || return 1") < linux.index(
        "cleanup_project_resources false"
    )
    assert windows.index('"telegram-media-bot", "doctor"') < windows.index(
        "try { Invoke-TmbCleanup"
    )


def test_runtime_image_guarantees_compatible_7zip_commands_and_shared_identity() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))

    assert "command -v 7zz >/dev/null" in dockerfile
    assert 'ln -sfn "$(command -v 7z)" /usr/local/bin/7zz' in dockerfile
    assert "ARG APP_UID=10001" in dockerfile
    assert "ARG APP_GID=10001" in dockerfile
    assert compose["x-app-common"]["build"]["args"]["APP_UID"] == "${APP_UID:-10001}"
    assert compose["x-app-common"]["build"]["args"]["APP_GID"] == "${APP_GID:-10001}"


def test_linux_installer_and_updater_install_command_and_repair_permissions() -> None:
    installer = Path("install.sh").read_text(encoding="utf-8")
    updater = Path("scripts/tmb.sh").read_text(encoding="utf-8")

    assert 'sudo ln -sfn "$INSTALL_DIR/scripts/tmb.sh" "$TMB_BIN_DIR/tmb"' in installer
    assert "repair_tmb_command" in updater
    assert "normalize_runtime_permissions" in updater
    assert "docker run --rm --user 0 --entrypoint sh" in updater
    assert "find /workspace/data /workspace/backups -type f -exec chmod 600" in updater
    assert 'connection.execute("PRAGMA journal_mode = WAL")' in updater
    assert "verify_services_healthy" in updater
    assert "rollback_application_files" in updater
    assert 'chmod 755 "$target"' in updater


def test_linux_release_archive_bootstraps_safely_from_v1_0_2_updater() -> None:
    builder = Path("scripts/build_release_archives.sh").read_text(encoding="utf-8")

    assert '"$TEMPORARY_DIRECTORY/tree/$PREFIX/scripts/tmb.sh"' in builder
    assert "ln -s tmb-current.sh" in builder
    assert "scripts/tmb-current.sh" in builder
    assert '"${TEMPORARY_DIRECTORY:?}/tree/$PREFIX/data"' in builder
    assert "chmod 755" in builder
    assert "--sort=name" in builder
    assert "gzip -n -9" in builder


def test_worker_enables_official_arq_abort_support() -> None:
    worker_settings = Path("src/telegram_media_bot/workers/settings.py").read_text(encoding="utf-8")
    queue_adapter = Path("src/telegram_media_bot/infrastructure/queue/arq_queue.py").read_text(
        encoding="utf-8"
    )

    assert "allow_abort_jobs: bool = True" in worker_settings
    assert "await job.abort(" in queue_adapter
