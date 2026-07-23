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
