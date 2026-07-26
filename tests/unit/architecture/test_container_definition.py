import re
from pathlib import Path

import yaml

TELEGRAM_BOT_API_PARENT_COMMIT = (
    "adfd7f6a8e990272851777eeb3ae0def4216f161"  # pragma: allowlist secret
)
PRODUCTION_RELEASE_CONDITION = (
    "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
)


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
    assert any(mount.startswith("/tmp:") for mount in common["tmpfs"])


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

    assert expected_tags("v1.0.0") == [
        f"{image}:v1.0.0",
        f"{image}:1.0.0",
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


def test_release_waits_for_published_image_smoke_test_and_attaches_verified_assets() -> None:
    workflow = Path(".github/workflows/publish-container.yml").read_text(encoding="utf-8")

    assert 'if tag != f"v{version}":' in workflow
    assert "Tag {tag} does not match pyproject.toml version {version}" in workflow
    assert 'docker run --rm "$image" telegram-media-bot --help' in workflow
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
