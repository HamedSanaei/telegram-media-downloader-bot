"""Credential resolution and operator attestation tests (T019)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_media_bot.application.services.credential_resolution import CredentialResolver
from telegram_media_bot.application.services.credential_vault import CredentialVault
from telegram_media_bot.application.services.operator_attestation import (
    OperatorPublicAttestationService,
)
from telegram_media_bot.domain.credential_resolution import (
    CredentialContext,
    CredentialKind,
    CredentialPolicy,
    CredentialResolutionCategory,
    operator_ig_records_verifier,
)
from telegram_media_bot.domain.errors import (
    CredentialExpiredError,
    CredentialGenerationMismatchError,
    OperatorAttestationStaleError,
    OperatorUnattestedError,
)
from telegram_media_bot.domain.instagram_credentials import InstagramCredentialState
from telegram_media_bot.infrastructure.credentials.key_ring import CredentialCryptor, VaultKeyRing
from telegram_media_bot.infrastructure.credentials.materializer import RestrictedCookieMaterializer
from telegram_media_bot.infrastructure.persistence.sqlite_instagram_credentials import (
    SqliteInstagramCredentialRepository,
)
from telegram_media_bot.infrastructure.persistence.sqlite_operator_attestation import (
    SqliteOperatorAttestationRepository,
)


def _key() -> str:
    return secrets.token_bytes(32).hex()


def _vault_and_materializer(
    tmp_path: Path,
) -> tuple[CredentialVault, RestrictedCookieMaterializer]:
    repo = SqliteInstagramCredentialRepository(tmp_path / "creds.sqlite3")
    repo.initialize()
    ring = VaultKeyRing.from_hex_material(active_key_id="k1", active_key=_key())
    vault = CredentialVault(repo, CredentialCryptor(ring))
    materializer = RestrictedCookieMaterializer(repo, CredentialCryptor(ring))
    return vault, materializer


def _attestation_service(
    tmp_path: Path, *, key: bytes | None = None
) -> tuple[SqliteOperatorAttestationRepository, OperatorPublicAttestationService]:
    repo = SqliteOperatorAttestationRepository(tmp_path / "attestation.sqlite3")
    repo.initialize()
    return repo, OperatorPublicAttestationService(repo, attestation_key=key)


# --- domain verifier ---------------------------------------------------------


def test_verifier_is_keyed_and_order_independent() -> None:
    records = ("sessionid=abc", "ds_user_id=123")
    unkeyed = operator_ig_records_verifier(records, key=None)
    keyed = operator_ig_records_verifier(records, key=b"k")
    assert unkeyed != keyed
    # Sorted normalization: same set in any order produces the same digest.
    assert operator_ig_records_verifier(tuple(reversed(records)), key=None) == unkeyed
    assert operator_ig_records_verifier(records, key=b"k") == keyed


def test_verifier_detects_record_tampering() -> None:
    records = ("sessionid=abc", "ds_user_id=123")
    before = operator_ig_records_verifier(records, key=b"k")
    after = operator_ig_records_verifier(("sessionid=xyz", "ds_user_id=123"), key=b"k")
    assert before != after


# --- attestation service -----------------------------------------------------


def test_attest_then_require_valid_passes(tmp_path: Path) -> None:
    _repo, service = _attestation_service(tmp_path)
    service.attest(
        instagram_records=("sessionid=a", "ds_user_id=1"),
        actor_role="operator",
        following_count=0,
        identity_verified=True,
    )
    attestation = service.require_valid(instagram_records=("sessionid=a", "ds_user_id=1"))
    assert attestation.operator_generation == 1
    assert service.current_generation() == 1
    assert service.is_valid(instagram_records=("sessionid=a", "ds_user_id=1"))


def test_unattested_account_fails_closed(tmp_path: Path) -> None:
    _repo, service = _attestation_service(tmp_path)
    with pytest.raises(OperatorUnattestedError):
        service.require_valid(instagram_records=("sessionid=a",))
    assert not service.is_valid(instagram_records=("sessionid=a",))
    assert service.current_generation() is None


def test_changed_records_invalidate_attestation(tmp_path: Path) -> None:
    _repo, service = _attestation_service(tmp_path)
    service.attest(
        instagram_records=("sessionid=a", "ds_user_id=1"),
        actor_role="operator",
        following_count=0,
        identity_verified=True,
    )
    with pytest.raises(OperatorAttestationStaleError):
        service.require_valid(instagram_records=("sessionid=b", "ds_user_id=1"))
    assert not service.is_valid(instagram_records=("sessionid=b", "ds_user_id=1"))


def test_attestation_generation_increments(tmp_path: Path) -> None:
    _repo, service = _attestation_service(tmp_path)
    assert (
        service.attest(
            instagram_records=("a",),
            actor_role="operator",
            following_count=0,
            identity_verified=True,
        ).operator_generation
        == 1
    )
    assert (
        service.attest(
            instagram_records=("a",),
            actor_role="operator",
            following_count=0,
            identity_verified=True,
        ).operator_generation
        == 2
    )
    assert service.current_generation() == 2
    # The newest attestation is authoritative.
    assert service.require_valid(instagram_records=("a",)).operator_generation == 2


def test_attestation_persists_across_service_instances(tmp_path: Path) -> None:
    repo, service = _attestation_service(tmp_path)
    service.attest(
        instagram_records=("sessionid=a",),
        actor_role="operator",
        following_count=0,
        identity_verified=True,
    )
    reloaded = OperatorPublicAttestationService(repo)
    assert reloaded.require_valid(instagram_records=("sessionid=a",)).operator_generation == 1


def test_attestation_uses_configured_key(tmp_path: Path) -> None:
    # A service with a different key cannot validate records attested under another key.
    _repo, service = _attestation_service(tmp_path, key=b"first-key")
    service.attest(
        instagram_records=("sessionid=a",),
        actor_role="operator",
        following_count=0,
        identity_verified=True,
    )
    _repo2, service2 = _attestation_service(tmp_path, key=b"second-key")
    with pytest.raises(OperatorAttestationStaleError):
        service2.require_valid(instagram_records=("sessionid=a",))


# --- resolver ----------------------------------------------------------------


def test_resolver_materializes_user_cookie_inside_workspace(tmp_path: Path) -> None:
    vault, materializer = _vault_and_materializer(tmp_path)
    vault.store_session(7, b"alice-session")
    resolver = CredentialResolver(vault, materializer)
    workspace = tmp_path / "job"
    context = CredentialContext(
        kind=CredentialKind.USER_INSTAGRAM, policy=CredentialPolicy.USER_ONLY, user_generation=1
    )
    with resolver.resolve(owner_user_id=7, context=context, workspace=workspace) as resolved:
        assert resolved.context.kind is CredentialKind.USER_INSTAGRAM
        assert resolved.materialized_cookie_path is not None
        materialized = Path(resolved.materialized_cookie_path)
        assert materialized.is_relative_to(workspace)
        assert materialized.is_file()
    # Materialized plaintext is cleaned up on exit; no ciphertext leaks into the workspace.
    assert not materialized.exists()


def test_resolver_user_context_requires_owner(tmp_path: Path) -> None:
    vault, materializer = _vault_and_materializer(tmp_path)
    resolver = CredentialResolver(vault, materializer)
    context = CredentialContext(
        kind=CredentialKind.USER_INSTAGRAM, policy=CredentialPolicy.USER_ONLY
    )
    with (
        pytest.raises(ValueError),
        resolver.resolve(owner_user_id=None, context=context, workspace=tmp_path / "job"),
    ):
        pass


def test_resolver_rejects_stale_user_generation(tmp_path: Path) -> None:
    vault, materializer = _vault_and_materializer(tmp_path)
    vault.store_session(7, b"alice-session")
    resolver = CredentialResolver(vault, materializer)
    context = CredentialContext(
        kind=CredentialKind.USER_INSTAGRAM,
        policy=CredentialPolicy.USER_ONLY,
        user_generation=99,
    )
    with (
        pytest.raises(CredentialGenerationMismatchError),
        resolver.resolve(owner_user_id=7, context=context, workspace=tmp_path / "job"),
    ):
        pass


def test_resolver_rejects_expired_user_session(tmp_path: Path) -> None:
    vault, materializer = _vault_and_materializer(tmp_path)
    vault.store_session(7, b"alice-session")
    vault.set_state_category(7, InstagramCredentialState.EXPIRED)
    resolver = CredentialResolver(vault, materializer)
    context = CredentialContext(
        kind=CredentialKind.USER_INSTAGRAM, policy=CredentialPolicy.USER_ONLY, user_generation=1
    )
    with (
        pytest.raises(CredentialExpiredError),
        resolver.resolve(owner_user_id=7, context=context, workspace=tmp_path / "job"),
    ):
        pass


def test_resolver_operator_context_never_touches_user_material(tmp_path: Path) -> None:
    vault, materializer = _vault_and_materializer(tmp_path)
    resolver = CredentialResolver(vault, materializer)
    context = CredentialContext(
        kind=CredentialKind.OPERATOR_PUBLIC, policy=CredentialPolicy.OPERATOR_PUBLIC
    )
    with resolver.resolve(owner_user_id=7, context=context, workspace=tmp_path / "job") as resolved:
        assert resolved.materialized_cookie_path is None
    assert not (tmp_path / "job").exists()


def test_resolver_none_context_is_credential_free(tmp_path: Path) -> None:
    vault, materializer = _vault_and_materializer(tmp_path)
    resolver = CredentialResolver(vault, materializer)
    with resolver.resolve(
        owner_user_id=None, context=CredentialContext.none(), workspace=tmp_path / "job"
    ) as resolved:
        assert resolved.context.kind is CredentialKind.NONE
        assert resolved.materialized_cookie_path is None


# --- typed routing categories -------------------------------------------------


def test_typed_failure_categories_allow_public_fallback_only_when_typed() -> None:
    assert CredentialResolutionCategory.EXPIRED.is_credential_or_session_failure
    assert CredentialResolutionCategory.CHALLENGE_REQUIRED.is_credential_or_session_failure
    assert CredentialResolutionCategory.REVOKED.is_credential_or_session_failure
    assert CredentialResolutionCategory.DISCONNECTED.is_credential_or_session_failure
    assert CredentialResolutionCategory.ADAPTER_AUTH.is_credential_or_session_failure
    for category in (
        CredentialResolutionCategory.NO_CREDENTIAL,
        CredentialResolutionCategory.OWNER_MISMATCH,
        CredentialResolutionCategory.GENERATION_MISMATCH,
        CredentialResolutionCategory.LEASE_BUSY,
        CredentialResolutionCategory.DECRYPT_FAILED,
        CredentialResolutionCategory.OPERATOR_UNATTESTED,
        CredentialResolutionCategory.OPERATOR_ATTESTATION_STALE,
        CredentialResolutionCategory.OPERATOR_EXPIRED,
        CredentialResolutionCategory.MATERIALIZATION_LOCAL,
    ):
        assert not category.is_credential_or_session_failure


def test_context_none_shortcut() -> None:
    context = CredentialContext.none()
    assert context.kind is CredentialKind.NONE
    assert context.user_generation is None
    assert context.operator_generation is None


def test_attested_at_is_preserved_with_clock_injection(tmp_path: Path) -> None:
    _repo, service = _attestation_service(tmp_path)
    moment = datetime.now(UTC) - timedelta(minutes=5)
    attestation = service.attest(
        instagram_records=("sessionid=a",),
        actor_role="operator",
        following_count=0,
        identity_verified=True,
        now=moment,
    )
    assert attestation.attested_at == moment


@pytest.mark.parametrize(
    ("following_count", "identity_verified"),
    [(1, True), (None, True), (0, False)],
)
def test_attestation_requires_verified_zero_follow_account(
    tmp_path: Path, following_count: int | None, identity_verified: bool
) -> None:
    _repo, service = _attestation_service(tmp_path)
    with pytest.raises(OperatorUnattestedError):
        service.attest(
            instagram_records=("sessionid=a",),
            actor_role="operator",
            following_count=following_count,
            identity_verified=identity_verified,
        )
