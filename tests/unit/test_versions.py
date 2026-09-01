"""Central PEP 440 version comparison regression tests."""

from __future__ import annotations

import pytest

from telegram_media_bot.versions import is_valid_version, versions_equal


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # RC pre-release forms are PEP 440-equivalent (the doctor's real-world mismatch).
        ("1.4.0-rc.1", "1.4.0rc1"),
        ("1.4.0rc1", "1.4.0-rc.1"),
        # Stable versions still compare correctly.
        ("1.4.0", "1.4.0"),
        ("1.3.8", "1.3.8"),
        ("1.2.3", "1.2.3.0"),
        ("v1.4.0", "1.4.0"),
        # Case differences in pre-release labels are normalized away.
        ("1.4.0-RC.1", "1.4.0-rc.1"),
    ],
)
def test_versions_equal_accepts_pep440_equivalents(left: str, right: str) -> None:
    assert versions_equal(left, right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("1.4.0", "1.4.1"),
        ("1.4.0-rc.1", "1.4.0"),
        ("1.3.8", "1.4.0-rc.1"),
        ("2.0.0", "1.4.0rc1"),
    ],
)
def test_genuinely_different_versions_are_unequal(left: str, right: str) -> None:
    assert not versions_equal(left, right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Malformed input must fail safely (return False), never raise.
        ("not-a-version", "1.4.0"),
        ("1.4.0", "not-a-version"),
        ("", "1.4.0"),
        ("1.4.0", ""),
        ("1.4.0.1.2.3.4.5.6.7", "1.4.0"),
    ],
)
def test_malformed_versions_fail_safely(left: str, right: str) -> None:
    assert versions_equal(left, right) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.4.0", True),
        ("1.4.0-rc.1", True),
        ("1.4.0rc1", True),
        ("v1.4.0", True),
        ("not-a-version", False),
        ("", False),
        ("1.4.0-", False),
    ],
)
def test_is_valid_version(value: str, expected: bool) -> None:
    assert is_valid_version(value) is expected
