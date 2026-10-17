"""Instagram account connection service (T018).

Mints short-lived, single-use connection links on the bot side and runs the transient web login
flow against a replaceable acquirer, storing a successful session into the encrypted vault. Only
sanitized lifecycle state is exposed; passwords/2FA codes never leave the caller's transient
memory and are never durable.
"""

from __future__ import annotations

from collections.abc import Callable

from telegram_media_bot.application.ports.instagram_login import InstagramSessionAcquirer
from telegram_media_bot.application.services.credential_vault import CredentialVault
from telegram_media_bot.application.services.handoff import HandoffLinkService
from telegram_media_bot.domain.instagram_connection import InstagramLoginResult
from telegram_media_bot.domain.instagram_credentials import SafeCredentialView
from telegram_media_bot.domain.web_companion import HandoffPurpose


class InstagramConnectionService:
    def __init__(
        self,
        *,
        vault: CredentialVault,
        acquirer: InstagramSessionAcquirer,
        link_service: HandoffLinkService | None = None,
        connect_base_url: str = "",
        acquirer_factory: Callable[[], InstagramSessionAcquirer] | None = None,
    ) -> None:
        # The companion process composes this service WITHOUT the bot-side signer (least
        # privilege, T016): link minting is then unavailable, which is exactly right.
        self._link_service = link_service
        self._base_url = connect_base_url.rstrip("/")
        self._vault = vault
        self._acquirer = acquirer
        self._acquirer_factory = acquirer_factory

    def new_login_attempt(self) -> InstagramConnectionService:
        """Give each browser its own provider cookie jar and pending challenge."""
        if self._acquirer_factory is None:
            return self
        return InstagramConnectionService(vault=self._vault, acquirer=self._acquirer_factory())

    def acquire_login(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        twofa_code: str | None = None,
    ) -> InstagramLoginResult:
        """Blocking provider operation only; cancellation must not publish a session."""
        return self._acquirer.step(username=username, password=password, twofa_code=twofa_code)

    def store_login(self, owner_user_id: int, result: InstagramLoginResult) -> None:
        if result.connected and result.session_bytes is not None:
            self._vault.store_session(owner_user_id, result.session_bytes)

    def create_connect_link(self, owner_user_id: int) -> str:
        """Mint a signed, single-use link with the handoff token in the URL fragment."""
        if self._link_service is None:
            raise RuntimeError("link minting is unavailable in this composition")
        token = self._link_service.create(
            purpose=HandoffPurpose.INSTAGRAM_CONNECT, owner_user_id=owner_user_id
        )
        return f"{self._base_url}/instagram/connect#handoff={token}"

    def submit_login(
        self,
        owner_user_id: int,
        *,
        username: str | None = None,
        password: str | None = None,
        twofa_code: str | None = None,
    ) -> InstagramLoginResult:
        """Submit one transient login step and, on success, store the encrypted session.

        The acquirer's success already proves a real authenticated session; the plaintext
        credential material (username/password/2FA plus the one-shot session bytes) is held by
        the caller and immediately released after this call returns.
        """
        result = self.acquire_login(username=username, password=password, twofa_code=twofa_code)
        self.store_login(owner_user_id, result)
        return result

    def status(self, owner_user_id: int) -> SafeCredentialView | None:
        return self._vault.get_view(owner_user_id)

    def disconnect(self, owner_user_id: int) -> SafeCredentialView:
        return self._vault.disconnect(owner_user_id)


__all__ = ["InstagramConnectionService"]
