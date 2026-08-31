"""Owner-safe credential resolver (T019).

Resolves an explicit ``CredentialContext`` into a short-lived, leased attempt handle. A user
Instagram context materializes the encrypted vault session inside the exact job workspace with
restrictive permissions and releases it on every exit path; operator and none contexts resolve
without touching user material. The resolver never chooses policy — callers (T020/T021 policy)
decide which context to request.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from telegram_media_bot.application.services.credential_vault import CredentialVault
from telegram_media_bot.domain.credential_resolution import (
    CredentialContext,
    CredentialKind,
    ResolvedCredential,
)
from telegram_media_bot.infrastructure.credentials.materializer import RestrictedCookieMaterializer


class CredentialResolver:
    def __init__(self, vault: CredentialVault, materializer: RestrictedCookieMaterializer) -> None:
        self._vault = vault
        self._materializer = materializer

    @contextmanager
    def resolve(
        self,
        *,
        owner_user_id: int | None,
        context: CredentialContext,
        workspace: Path,
    ) -> Iterator[ResolvedCredential]:
        """Yield a bounded attempt handle for the requested context.

        ``owner_user_id`` is required for USER_INSTAGRAM contexts and is the only way to reach a
        user credential; it is never derived from a username or URL.
        """
        if context.kind is CredentialKind.USER_INSTAGRAM:
            if owner_user_id is None:
                raise ValueError("owner_user_id is required for a user credential context")
            with self._materializer.open(
                owner_user_id=owner_user_id,
                workspace=workspace,
                expected_generation=context.user_generation,
            ) as path:
                yield ResolvedCredential(context, materialized_cookie_path=str(path))
            return
        if context.kind is CredentialKind.OPERATOR_PUBLIC:
            # Operator cookies are read from the canonical operator file by the engine; the
            # resolver only confirms the caller already validated the attestation.
            yield ResolvedCredential(context, materialized_cookie_path=None)
            return
        yield ResolvedCredential(context, materialized_cookie_path=None)


__all__ = ["CredentialResolver"]
