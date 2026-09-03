from __future__ import annotations

import json
import re
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

_SPEC = spec_from_file_location("check_release_policy", "scripts/check_release_policy.py")
assert _SPEC is not None and _SPEC.loader is not None
_POLICY_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_POLICY_MODULE)

assert_release_allowed = _POLICY_MODULE.assert_release_allowed
blocked_release = _POLICY_MODULE.blocked_release
load_release_policy = _POLICY_MODULE.load_release_policy
normalize_release_version = _POLICY_MODULE.normalize_release_version


def test_withdrawn_release_policy_blocks_only_1_3_7() -> None:
    policy = load_release_policy()

    assert normalize_release_version("v1.3.7") == "1.3.7"
    assert blocked_release("1.3.7", policy) is not None
    assert blocked_release("v1.3.7", policy) is not None
    for allowed in ("1.3.6", "v1.3.8", "1.3.9", "v2.0.0"):
        assert blocked_release(allowed, policy) is None
        assert_release_allowed(allowed, policy)

    with pytest.raises(ValueError, match=r"Release 1\.3\.7 is blocked"):
        assert_release_allowed("v1.3.7", policy)


def test_standalone_bootstrap_policy_snapshots_match_canonical_policy() -> None:
    policy = json.loads(Path("release-policy.json").read_text(encoding="utf-8"))
    expected = set(policy["blocked_releases"])
    scripts = (
        Path("install.sh"),
        Path("scripts/lib/common.sh"),
        Path("install.ps1"),
        Path("scripts/tmb.ps1"),
    )

    for script in scripts:
        text = script.read_text(encoding="utf-8")
        match = re.search(
            r"BLOCKED_?RELEASE_?VERSIONS\s*=\s*(?:@)?\(([^)]*)\)",
            text,
            flags=re.IGNORECASE,
        )
        assert match is not None, f"missing embedded release policy in {script}"
        actual = set(re.findall(r"\"([^\"]+)\"", match.group(1)))
        assert actual == expected, f"embedded release policy drift in {script}"
        assert re.search(r"assert[-_]release[-_]?allowed", text, flags=re.IGNORECASE) or (
            script.name == "common.sh"
            and re.search(
                r"assert[-_]release[-_]?allowed",
                Path("scripts/lib/update.sh").read_text(encoding="utf-8"),
                flags=re.IGNORECASE,
            )
        )


def test_release_build_and_publish_paths_enforce_canonical_policy() -> None:
    builder = Path("scripts/build_release_archives.sh").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/publish-container.yml").read_text(encoding="utf-8")

    command = 'python scripts/check_release_policy.py --version "$PROJECT_VERSION"'
    assert command in builder
    assert 'python scripts/check_release_policy.py --version "$GITHUB_REF_NAME"' in workflow
    assert builder.index(command) < builder.index("git archive --format=tar")
    assert workflow.index("check_release_policy.py") < workflow.index("docker/setup-buildx-action")
