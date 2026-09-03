import json
import os
import re
import shutil
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
        "scripts/tests/test_readonly_logger_preflight.sh",
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


def test_windows_updater_keeps_strong_post_install_logger_doctor() -> None:
    updater = Path("scripts/tmb.ps1").read_text(encoding="utf-8")

    # Windows does not run the Linux read-only candidate bind-mount preflight.
    # Its post-install verification must keep the full SQLite health snapshot.
    assert '"telegram-media-bot", "doctor", "--config", "/app/config.yaml"' in updater
    assert "--read-only-runtime" not in updater


def test_app_containers_are_read_only_and_drop_capabilities() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    common = compose["x-app-common"]

    assert common["read_only"] is True
    assert common["cap_drop"] == ["ALL"]
    assert common["security_opt"] == ["no-new-privileges:true"]
    assert common["restart"] == "unless-stopped"
    assert any(mount.startswith("/tmp:") for mount in common["tmpfs"])
    assert "./config.yaml:/app/config.yaml:ro" in common["volumes"]
    assert "./data:/data" in common["volumes"]
    for service_name in ("bot", "worker"):
        assert compose["services"][service_name]["volumes"] == common["volumes"]
    assert compose["services"]["worker"]["cpus"] == "${TMB_WORKER_CPUS:-0}"


def test_every_production_service_uses_unless_stopped_restart_policy() -> None:
    """All always-on services must recover after Docker daemon/host restarts (regression).

    `on-failure` policies are ignored by the daemon after a restart/reboot, which previously
    left bot/worker/local-api offline while Redis (already `unless-stopped`) came back.
    """
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    common = compose["x-app-common"]

    # The shared anchor is the single source of truth for application services.
    assert common["restart"] == "unless-stopped"
    for service_name in ("bot", "worker", "local-api"):
        # Anchor merge is resolved by the loader, so the effective policy must match.
        assert compose["services"][service_name]["restart"] == "unless-stopped"
    assert compose["services"]["redis"]["restart"] == "unless-stopped"


def test_rendered_compose_config_assigns_unless_stopped_to_all_services() -> None:
    """The effective rendered Compose config must apply `unless-stopped` to every service."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    rendered = subprocess.run(
        ["docker", "compose", "--profile", "local-api", "config", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(rendered.stdout)["services"]
    for service_name in ("bot", "worker", "local-api", "redis"):
        assert services[service_name]["restart"] == "unless-stopped"


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


TELEGRAM_BOT_API_IMAGE = "ghcr.io/hamedsanaei/telegram-bot-api"
TELEGRAM_BOT_API_DIGEST = "sha256:36f4813c3feeb09a09918caa8617d8e217784019065298c6ad1bca2ca2dea826"


def test_telegram_bot_api_is_consumed_as_immutable_artifact() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert re.search(
        rf"^ARG TELEGRAM_BOT_API_IMAGE={re.escape(TELEGRAM_BOT_API_IMAGE)}"
        rf"@{re.escape(TELEGRAM_BOT_API_DIGEST)}$",
        dockerfile,
        re.MULTILINE,
    )
    assert "FROM ${TELEGRAM_BOT_API_IMAGE} AS telegram-bot-api" in dockerfile
    copy = dockerfile.split("COPY --from=telegram-bot-api", maxsplit=1)[1]
    assert "/telegram-bot-api" in copy
    assert "/usr/local/bin/telegram-bot-api" in copy


def test_application_build_never_compiles_telegram_bot_api() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    for forbidden in (
        "git clone",
        "git submodule update",
        "cmake --build",
        "cmake -S",
        "gperf",
        "libssl-dev",
        "zlib1g-dev",
        "telegram-bot-api-build",
    ):
        assert forbidden not in dockerfile


def test_telegram_source_build_is_isolated_in_the_dedicated_dockerfile() -> None:
    main = Path("Dockerfile").read_text(encoding="utf-8")
    dedicated = Path("Dockerfile.telegram-bot-api").read_text(encoding="utf-8")
    match = re.search(r"^ARG TELEGRAM_BOT_API_REF=(?P<ref>[0-9a-f]+)$", dedicated, re.MULTILINE)

    assert match is not None
    assert re.fullmatch(r"[0-9a-f]{40}", match["ref"])
    assert match["ref"] == TELEGRAM_BOT_API_PARENT_COMMIT
    clone = "git clone --filter=blob:none --no-checkout"
    checkout = 'git checkout --detach "${TELEGRAM_BOT_API_REF}"'
    submodules = "git submodule update --init --recursive"
    assert dedicated.index(clone) < dedicated.index(checkout) < dedicated.index(submodules)
    assert "cmake --build build --target install" in dedicated
    assert "git clone --filter=blob:none --recursive" not in dedicated
    for marker in (clone, submodules, "cmake --build"):
        assert marker not in main


def test_telegram_artifact_workflow_is_manual_only() -> None:
    workflow = yaml.load(
        Path(".github/workflows/build-telegram-bot-api.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert workflow["on"] == {"workflow_dispatch": ""}
    assert "push" not in workflow["on"]
    assert workflow["permissions"] == {"contents": "read"}


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
    steps = workflow["jobs"]["docker-runtime"]["steps"]
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
    assert any("docker compose --profile local-api config" in run for run in runs)
    assert any("unless-stopped" in run for run in runs)
    assert any(
        "docker run --rm telegram-media-downloader-bot:ci telegram-media-bot --help" in run
        for run in runs
    )
    assert any("native_selection_smoke" in run for run in runs)
    assert any("native_ui_smoke" in run for run in runs)
    assert any("gallery_dl --config-ignore --version" in run for run in runs)
    assert any("infrastructure.gallerydl.smoke" in run for run in runs)
    assert any("gallery-dl-GPL-2.0.txt" in run for run in runs)
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
    # The privileged updater matrix is a separate conditional lane in T033; runtime image validation
    # must not silently include it, and updater integration must live in its dedicated lane.
    assert all("test_tmb_upgrade_integration.sh" not in run for run in runs)
    assert all("docker compose --profile local-api build" not in run for run in runs)


def test_updater_integration_lane_retains_full_historical_matrix() -> None:
    """The conditional updater-integration lane still owns every privileged updater scenario."""
    workflow = yaml.load(
        Path(".github/workflows/ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert "updater-integration" in workflow["jobs"]
    steps = workflow["jobs"]["updater-integration"]["steps"]
    runs = [step.get("run", "") for step in steps]
    envs = [str(step.get("env", "")) for step in steps]
    combined = "\n".join(runs + envs)

    assert any("test_local_api_readiness.sh" in run for run in runs)
    assert any("test_readonly_logger_preflight.sh" in run for run in runs)
    for marker in (
        "TMB_TEST_PREVIOUS_VERSION=1.0.2",
        "TMB_TEST_PREVIOUS_VERSION=1.2.1",
        "TMB_TEST_PREVIOUS_VERSION=1.3.0",
        "TMB_TEST_PREVIOUS_VERSION=1.3.1",
        "TMB_USE_RELEASE_UPDATER_ASSET=1",
        "TMB_TEST_ACTIVE_LOCAL_API_LOG_WRITER=1",
        "TMB_TEST_INITIAL_SERVICE_STATE=all-running",
        "TMB_TEST_INITIAL_SERVICE_STATE=no-local-api",
        "TMB_TEST_INITIAL_SERVICE_STATE=no-bot",
        "TMB_TEST_INITIAL_SERVICE_STATE=mixed",
        "TMB_TEST_UPDATER_FAILURE_STAGE=backup",
        "TMB_TEST_UPDATER_FAILURE_STAGE=offline-doctor",
        "TMB_TEST_UPDATER_FAILURE_STAGE=online-doctor",
        "RUN_PRIVILEGED_UPGRADE_TESTS",
    ):
        assert marker in combined


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

    assert expected_tags("v1.1.0") == [
        f"{image}:v1.1.0",
        f"{image}:1.1.0",
        f"{image}:1.1",
        f"{image}:latest",
    ]
    assert f"{image}:latest" not in expected_tags("v1.1.0-beta.1")
    assert "type=raw,value=${{ github.ref_name }}" in workflow
    assert "type=semver,pattern={{version}},value=${{ github.ref_name }}" in workflow
    assert "type=semver,pattern={{major}}.{{minor}},value=${{ github.ref_name }}" in workflow
    assert "type=raw,value=latest,enable=${{ !contains(github.ref_name, '-') }}" in workflow
    assert "platforms: linux/amd64" in workflow
    assert "linux/arm64" not in workflow


SEMVER_TAG_PATTERN = re.compile(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")


def test_release_tag_exactly_matches_project_version() -> None:
    """The publish contract is generic: tag == v{pyproject version}, valid SemVer.

    Deliberately version-agnostic (no hard-coded release number) so stable and
    pre-release bumps such as 1.4.0-rc.1 keep this test green while unresolved
    drift between sources of truth is caught. The same SemVer pattern is enforced
    by the publish workflow's inline guard.
    """
    from telegram_media_bot.versions import is_valid_version

    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    workflow = Path(".github/workflows/publish-container.yml").read_text(encoding="utf-8")
    version = project["project"]["version"]
    tag = f"v{version}"

    # The package must carry a syntactically valid PEP 440 version.
    assert is_valid_version(version)
    # The in-code version must never drift from pyproject.toml.
    assert __version__ == version
    # The release tag is exactly v{version}.
    assert tag == f"v{version}"
    assert SEMVER_TAG_PATTERN.fullmatch(tag) is not None
    # The workflow guard rejects any tag that does not match pyproject.toml.
    assert 'if tag != f"v{version}":' in workflow
    assert "Release tag is not valid SemVer" in workflow


def test_release_waits_for_published_image_smoke_test_and_attaches_verified_assets() -> None:
    workflow = Path(".github/workflows/publish-container.yml").read_text(encoding="utf-8")

    assert 'if tag != f"v{version}":' in workflow
    assert "Tag {tag} does not match pyproject.toml version {version}" in workflow
    assert 'docker run --rm "$image" telegram-media-bot --help' in workflow
    assert "native_selection_smoke" in workflow
    assert "native_ui_smoke" in workflow
    assert "gallery_dl --config-ignore --version" in workflow
    assert "infrastructure.gallerydl.smoke" in workflow
    assert "gallery-dl-GPL-2.0.txt" in workflow
    assert "command -v ffmpeg" in workflow
    assert "command -v ffprobe" in workflow
    assert "command -v 7zz || command -v 7z" in workflow
    assert '"$seven_zip" t /tmp/smoke.zip.001' in workflow
    assert "telegram-media-bot doctor --config /app/config.example.yaml" in workflow
    assert "usage_chart_smoke" in workflow
    assert "--verify-uid 10001" in workflow
    assert "--network none --read-only" in workflow
    assert "test_tmb_upgrade_integration.sh" in workflow
    assert "test_readonly_logger_preflight.sh" in workflow
    assert "TMB_TEST_PREVIOUS_VERSION=1.2.1" in workflow
    assert "TMB_TEST_PREVIOUS_VERSION=1.3.0" in workflow
    assert "TMB_TEST_PREVIOUS_VERSION=1.3.1" in workflow
    assert "TMB_USE_RELEASE_UPDATER_ASSET=1" in workflow
    assert "TMB_TEST_ACTIVE_LOCAL_API_LOG_WRITER=1" in workflow
    assert "TMB_TEST_INITIAL_SERVICE_STATE=all-running" in workflow
    assert "TMB_TEST_INITIAL_SERVICE_STATE=no-local-api" in workflow
    assert "TMB_TEST_INITIAL_SERVICE_STATE=no-bot" in workflow
    assert "TMB_TEST_INITIAL_SERVICE_STATE=mixed" in workflow
    assert "TMB_TEST_UPDATER_FAILURE_STAGE=backup" in workflow
    assert "TMB_TEST_UPDATER_FAILURE_STAGE=offline-doctor" in workflow
    assert "TMB_TEST_UPDATER_FAILURE_STAGE=online-doctor" in workflow
    assert "tmb-updater.sh.sha256" in workflow
    assert "sha256sum --check tmb-updater.sh.sha256" in workflow
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
        "tmb-updater.sh",
        "tmb-updater.sh.sha256",
    ):
        assert asset in workflow


def test_management_cleanup_is_project_scoped_and_runs_after_update_verification() -> None:
    linux = Path("scripts/tmb.sh").read_text(encoding="utf-8")
    linux += "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(Path("scripts/lib").glob("*.sh"))
    )
    windows = Path("scripts/tmb.ps1").read_text(encoding="utf-8")
    repository = "ghcr.io/hamedsanaei/telegram-media-downloader-bot"

    for script in (linux, windows):
        assert repository in script
        assert "prune_old_project_images_after_success" in script
        assert "cleanup-workspaces" in script
        assert "docker image prune" not in script
        assert "docker system prune" not in script
        assert "docker volume prune" not in script

    updater = Path("scripts/lib/update.sh").read_text(encoding="utf-8")
    assert updater.index("verify_candidate_release_offline || return 1") < updater.index(
        "cleanup_project_resources false"
    )
    assert windows.index('"telegram-media-bot", "doctor"') < windows.index(
        "try { Invoke-TmbCleanup"
    )


def test_linux_update_preflight_uses_prepared_image_and_read_only_runtime_data() -> None:
    updater = Path("scripts/lib/update.sh").read_text(encoding="utf-8")
    preflight = updater.split("validate_prepared_release() {", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]

    assert 'prepared_image="$IMAGE_REPOSITORY:$RELEASE_VERSION"' in preflight
    assert 'docker pull "$prepared_image"' in preflight
    assert 'uid="$(runtime_identity APP_UID 10001)"' in preflight
    assert 'gid="$(runtime_identity APP_GID 10001)"' in preflight
    assert 'run_update_stage "candidate configuration preflight" docker run' in preflight
    assert '--rm --read-only --user "$uid:$gid"' in preflight
    assert "--tmpfs /tmp:rw,noexec,nosuid,size=16m,mode=1777" in preflight
    assert '-v "$ROOT_DIR/config.yaml:/app/config.yaml:ro"' in preflight
    assert '-v "$ROOT_DIR/data:/data:ro"' in preflight
    assert "--read-only-runtime" in preflight
    assert "--offline" in preflight
    assert '--expected-version "$RELEASE_VERSION"' in preflight
    assert "/var/run/docker.sock" not in preflight
    assert "/root" not in preflight
    assert updater.index("validate_prepared_release || return 1") < updater.index(
        'run_update_stage "consistent persistent-state backup" backup'
    )
    assert updater.index("validate_prepared_release || return 1") < updater.index(
        'run_update_stage "filesystem-writer service stop"'
    )


def test_linux_update_backup_is_offline_atomic_and_preserves_exact_service_state() -> None:
    updater = Path("scripts/lib/update.sh").read_text(encoding="utf-8")
    backup_script = Path("scripts/lib/backup.sh").read_text(encoding="utf-8")
    common = Path("scripts/lib/common.sh").read_text(encoding="utf-8")
    updater = common + "\n" + updater

    transaction = updater.split("perform_update() {", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    backup = backup_script.split("backup_archive() {", maxsplit=1)[1].split("\n}", maxsplit=1)[0]

    assert "PROJECT_SERVICES=(bot worker local-api redis)" in updater
    assert "FILESYSTEM_WRITER_SERVICES=(bot worker local-api)" in updater
    assert transaction.index("prepare_verified_release") < transaction.index(
        'run_update_stage "filesystem-writer service stop"'
    )
    assert transaction.index('run_update_stage "filesystem-writer service stop"') < (
        transaction.index('run_update_stage "consistent persistent-state backup" backup')
    )
    assert transaction.index('run_update_stage "consistent persistent-state backup" backup') < (
        transaction.index("UPDATE_APPLICATION_MUTATED=true")
    )
    assert transaction.index("verify_candidate_release_offline") < transaction.index(
        'start_services true "${PREVIOUS_WRITER_SERVICES[@]}"'
    )
    assert transaction.index('start_services true "${PREVIOUS_WRITER_SERVICES[@]}"') < (
        transaction.index("verify_restored_services_online")
    )
    assert transaction.index("verify_restored_services_online") < transaction.index(
        'run_update_stage "updated exact service-state verification"'
    )
    assert "--online-service local-api" in updater
    assert "--online-service bot" in updater
    assert 'temporary_archive="$(mktemp "backups/.tmb-' in backup
    assert 'mv -f -- "$temporary_archive" "$archive"' in backup
    assert "--exclude='data/telegram-bot-api/telegram-bot-api.log'" in backup
    assert "--exclude='*.log'" not in backup
    # Downloads enter the archive only through the explicit --include-downloads
    # opt-in; the default operational/migration contents never include them.
    # Both mentions live inside the --include-downloads opt-in branch.
    assert backup.count("data/downloads") == 2
    assert 'if [[ "$include_downloads" == "1" && -d data/downloads ]]; then' in backup
    assert 'backup_items+=("data/downloads")' in backup
    assert "data/temp" not in backup
    assert "verify_exact_project_service_state" in updater
    assert "PREVIOUS_PROJECT_SERVICES" in updater
    assert "PREVIOUS_WRITER_SERVICES" in updater


def test_windows_manual_backup_stops_writers_and_includes_logger_sqlite_state() -> None:
    updater = Path("scripts/tmb.ps1").read_text(encoding="utf-8")
    backup = updater.split("function New-TmbBackup {", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    consistent = updater.split("function New-ConsistentTmbBackup {", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]

    assert '"data/state"' in backup
    assert '"data/cookies"' in backup
    assert '"config.yaml"' in backup
    assert "Get-RunningApplicationServices" in consistent
    assert 'Invoke-Compose (@("stop", "-t", "45") + $PreviousServices)' in consistent
    assert consistent.index("Invoke-Compose") < consistent.index("New-TmbBackup")
    assert "Start-TmbServices -Services $PreviousServices" in consistent
    assert "Wait-TmbServicesHealthy -Services $PreviousServices" in consistent
    assert '"backup" { New-ConsistentTmbBackup }' in updater


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
    updater = Path("scripts/lib/update.sh").read_text(encoding="utf-8")
    updater += "\n" + Path("scripts/lib/services.sh").read_text(encoding="utf-8")

    assert 'sudo ln -sfn "$INSTALL_DIR/scripts/tmb.sh" "$TMB_BIN_DIR/tmb"' in installer
    assert "repair_tmb_command" in updater
    assert "normalize_runtime_permissions" in updater
    assert "stat -c '%u:%g'" in updater
    assert "stat -c '%a'" in updater
    assert 'chown "$owner" "$env_path"' in updater
    assert 'chmod "$mode" "$env_path"' in updater
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
    assert '"$OUTPUT_DIRECTORY/tmb-updater.sh"' in builder
    assert "sha256sum tmb-updater.sh >tmb-updater.sh.sha256" in builder


def test_worker_enables_official_arq_abort_support() -> None:
    worker_settings = Path("src/telegram_media_bot/workers/settings.py").read_text(encoding="utf-8")
    queue_adapter = Path("src/telegram_media_bot/infrastructure/queue/arq_queue.py").read_text(
        encoding="utf-8"
    )

    assert "allow_abort_jobs: bool = True" in worker_settings
    assert "await job.abort(" in queue_adapter
