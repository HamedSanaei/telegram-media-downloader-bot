from pathlib import Path

import yaml


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
