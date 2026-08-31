"""Context-managed plaintext Netscape materialization inside a job workspace (T017)."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_media_bot.domain.errors import (
    CredentialMaterializationError,
    CredentialNotFoundError,
)
from telegram_media_bot.domain.instagram_credentials import (
    CREDENTIAL_PROVIDER,
    InstagramCredentialState,
    aad_for,
)
from telegram_media_bot.infrastructure.credentials.key_ring import CredentialCryptor
from telegram_media_bot.infrastructure.persistence.sqlite_instagram_credentials import (
    SqliteInstagramCredentialRepository,
)


class RestrictedCookieMaterializer:
    """Acquires a per-user lease, decrypts, and writes a restrictive Netscape file in the workspace.

    Guarantees the plaintext file and the lease are released on every exit path (normal return,
    exception, cancellation, timeout, and explicit cleanup). The file lives only inside the exact
    supplied job workspace; callers pass a job-owned directory and never a storage root.
    """

    def __init__(
        self,
        repository: SqliteInstagramCredentialRepository,
        cryptor: CredentialCryptor,
        *,
        filename: str = "cookies.txt",
        lease_ttl_seconds: int = 300,
    ) -> None:
        self._repository = repository
        self._cryptor = cryptor
        if not filename or any(part in filename for part in ("/", "\\", "..")):
            raise ValueError("materialized filename must be a bare safe name")
        self._filename = filename
        self._lease_ttl_seconds = lease_ttl_seconds

    @contextmanager
    def open(
        self,
        *,
        owner_user_id: int,
        workspace: Path,
        expected_generation: int | None = None,
    ) -> Iterator[Path]:
        credential = self._repository.get_credential_for_owner(owner_user_id)
        if credential is None:
            raise CredentialNotFoundError("no credential exists for the owner")
        if expected_generation is not None and credential.generation != expected_generation:
            from telegram_media_bot.domain.errors import CredentialGenerationMismatchError

            raise CredentialGenerationMismatchError(
                "credential generation changed before materialization"
            )
        if credential.state is InstagramCredentialState.REVOKED:
            from telegram_media_bot.domain.errors import CredentialRevokedError

            raise CredentialRevokedError("credential is revoked")
        if credential.state is InstagramCredentialState.DISCONNECTED:
            from telegram_media_bot.domain.errors import CredentialDisconnectedError

            raise CredentialDisconnectedError("credential is disconnected")
        if credential.state is InstagramCredentialState.EXPIRED:
            from telegram_media_bot.domain.errors import CredentialExpiredError

            raise CredentialExpiredError("credential session is expired")
        if credential.state is InstagramCredentialState.CHALLENGE_REQUIRED:
            from telegram_media_bot.domain.errors import CredentialChallengeRequiredError

            raise CredentialChallengeRequiredError("credential requires a challenge")
        now = datetime.now(UTC)
        lease = self._repository.acquire_lease(
            owner_user_id=owner_user_id,
            generation=credential.generation,
            expires_at=now + timedelta(seconds=self._lease_ttl_seconds),
            now=now,
        )
        target: Path | None = None
        plaintext: bytes | None = None
        try:
            if credential.envelope is None:
                raise CredentialNotFoundError("credential holds no ciphertext")
            aad = aad_for(
                provider=CREDENTIAL_PROVIDER,
                credential_id=credential.credential_id,
                owner_user_id=owner_user_id,
                generation=credential.generation,
            )
            plaintext = self._cryptor.decrypt(credential.envelope, aad=aad)
            target = self._write_restricted(workspace, plaintext)
            yield target
        finally:
            # Release plaintext buffer immediately and remove the file on every path.
            del plaintext
            if target is not None:
                _remove_restricted(target)
            self._repository.release_lease(lease.lease_id)

    def _write_restricted(self, workspace: Path, plaintext: bytes) -> Path:
        try:
            root = workspace.expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            target = (root / self._filename).resolve()
            if not target.is_relative_to(root):
                raise CredentialMaterializationError("materialized path escapes the workspace")
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, plaintext)
            finally:
                os.close(fd)
            _apply_private_permissions(target)
            return target
        except CredentialMaterializationError:
            raise
        except OSError as exc:
            raise CredentialMaterializationError(
                "plaintext materialization failed locally"
            ) from exc


def _apply_private_permissions(path: Path) -> None:
    try:
        stat_result = path.stat()
        if stat_result.st_mode & 0o077:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows ACL chmod surface is best-effort; the file is inside a private job workspace.
        pass


def _remove_restricted(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)


__all__ = ["RestrictedCookieMaterializer"]
