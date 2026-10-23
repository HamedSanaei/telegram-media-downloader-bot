"""Keep reviewed security floors in the reproducible dependency set.

These offline regression checks do not replace the live pip-audit CI gate.
"""

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version


@pytest.mark.parametrize(
    ("name", "minimum"),
    [("certifi", "2026.10.19"), ("pillow", "13.0.0"), ("virtualenv", "21.14.9")],
)
def test_lockfile_preserves_reviewed_security_floors(name: str, minimum: str) -> None:
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    versions = [
        Version(package["version"]) for package in lock["package"] if package["name"] == name
    ]

    assert versions, f"Missing reviewed dependency: {name}"
    assert all(version >= Version(minimum) for version in versions)


def test_pillow_requirement_allows_security_fix_and_rejects_old_major() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    requirement = next(
        requirement
        for value in project["project"]["dependencies"]
        if (requirement := Requirement(value)).name.casefold() == "pillow"
    )

    assert Version("13.0.0") in requirement.specifier
    assert Version("12.3.0") not in requirement.specifier
    assert Version("14.0.0") not in requirement.specifier
