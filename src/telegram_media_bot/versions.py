"""Central package-version comparison helpers.

All release/doctor/update version checks must go through this module so that
PEP 440-equivalent versions (for example ``1.4.0-rc.1`` and ``1.4.0rc1``) are
treated as equal instead of compared with raw string equality.
"""

from __future__ import annotations

from packaging.version import InvalidVersion, Version

__all__ = ["is_valid_version", "versions_equal"]


def versions_equal(left: str, right: str) -> bool:
    """Return whether two version strings are PEP 440-equivalent.

    Malformed input fails safely by returning ``False`` rather than raising, so
    callers (doctor/updater/installer checks) fail closed on bad versions.
    """
    try:
        return Version(left) == Version(right)
    except InvalidVersion:
        return False


def is_valid_version(value: str) -> bool:
    """Return whether ``value`` is a syntactically valid PEP 440 version."""
    try:
        Version(value)
    except InvalidVersion:
        return False
    return True
