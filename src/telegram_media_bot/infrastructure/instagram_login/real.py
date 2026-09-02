"""Real Instagram session acquirer (T025, sections 28-30).

Replaces the test-only fake in production composition. Performs a REAL login over HTTPS against
Instagram's public web login endpoints using an injectable synchronous transport:

1. ``GET /accounts/login/`` -> establish browser-style cookies (``csrftoken``, ``mid``).
2. ``POST /accounts/login/ajax/`` with the browser-style ``enc_password`` envelope. Credentials
   stay in the transient request and are never logged, persisted, or used as labels.
3. If ``two_factor_required``: the transient ``two_factor_identifier`` is kept in a bounded
   in-memory map (per username, expiring) and the caller is told to ask for the code.
4. ``POST /accounts/ajax/two_factor_ajax/`` with the code completes the checkpoint.
5. On authenticated success the session cookies (``sessionid``, ``ds_user_id``, ``rur``) are
   normalized into Netscape cookie-file bytes for the gallery-dl/yt-dlp materialization pipeline.

Fail-closed: any unexpected protocol shape, missing required cookie, transport failure, or
upstream rejection produces a typed sanitized denial. There is NO fake success fallback.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import timedelta

from telegram_media_bot.application.ports.instagram_login import InstagramSessionAcquirer
from telegram_media_bot.domain.instagram_connection import (
    InstagramLoginResult,
    LoginFailureCategory,
)
from telegram_media_bot.domain.web_companion import InstagramConnectStage
from telegram_media_bot.infrastructure.payments.base import (
    ProviderHttpRequester,
    ProviderHttpRequestError,
    StdlibHttpRequester,
)

_LOGIN_PAGE = "https://www.instagram.com/accounts/login/"
_LOGIN_AJAX = "https://www.instagram.com/accounts/login/ajax/"
_TWO_FACTOR_AJAX = "https://www.instagram.com/accounts/ajax/two_factor_ajax/"
_REQUIRED_COOKIES = ("sessionid", "ds_user_id", "rur")
_MAX_TRANSIENT_IDENTIFIERS = 64
_TRANSIENT_TTL = timedelta(minutes=10)


def _cookie_value(response_headers: Mapping[str, str], name: str) -> str | None:
    for raw in response_headers.get("set-cookie", "").split(","):
        cleaned = raw.strip()
        if cleaned.lower().startswith(name.lower() + "="):
            return cleaned.split(";", 1)[0].split("=", 1)[1].strip()
    return None


class RealInstagramSessionAcquirer(InstagramSessionAcquirer):
    """Real login acquirer with transient bounded 2FA identifier retention."""

    def __init__(
        self,
        *,
        requester: ProviderHttpRequester | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._requester = requester or StdlibHttpRequester()
        self._timeout = timeout_seconds
        # Transient per-username two_factory_identifier; never durable, bounded, expiring.
        self._identifiers: dict[str, tuple[float, str]] = {}

    # -- port -------------------------------------------------------------------

    def step(
        self,
        *,
        username: str | None,
        password: str | None,
        twofa_code: str | None,
    ) -> InstagramLoginResult:
        if twofa_code:
            if not username:
                return InstagramLoginResult(
                    InstagramConnectStage.DENIED, LoginFailureCategory.SESSION_REJECTED
                )
            return self._complete_checkpoint(username, twofa_code)
        if not username or not password:
            return InstagramLoginResult(
                InstagramConnectStage.DENIED, LoginFailureCategory.WRONG_CREDENTIALS
            )
        return self._login(username, password)

    # -- login step ----------------------------------------------------------------

    def _login(self, username: str, password: str) -> InstagramLoginResult:
        try:
            page = self._requester.request(
                "GET",
                _LOGIN_PAGE,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept-Language": "en",
                },
                timeout_seconds=self._timeout,
            )
        except ProviderHttpRequestError:
            return self._denied(LoginFailureCategory.NOT_AVAILABLE)
        if page.status >= 400:
            return self._denied(LoginFailureCategory.NOT_AVAILABLE)
        csrf = _cookie_value(page.headers, "csrftoken") or ""
        now_seconds = int(time.time())
        enc_password = f"#PWD_INSTAGRAM_BROWSER:0:{now_seconds}:{password}"
        try:
            response = self._requester.request(
                "POST",
                _LOGIN_AJAX,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "X-CSRFToken": csrf,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": _LOGIN_PAGE,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": _jar(page.headers),
                },
                form={
                    "username": username,
                    "enc_password": enc_password,
                    "queryParams": '{"source":"auth_switcher"}',
                    "optIntoOneTap": "false",
                },
                timeout_seconds=self._timeout,
            )
        except ProviderHttpRequestError:
            return self._denied(LoginFailureCategory.NOT_AVAILABLE)
        try:
            body = response.json()
        except ValueError:
            return self._denied(LoginFailureCategory.SESSION_REJECTED)
        if not isinstance(body, dict) or response.status >= 400:
            return self._denied(LoginFailureCategory.NOT_AVAILABLE)
        if body.get("authenticated") is True:
            return self._finalize(username, response.headers)
        if body.get("two_factor_required") is True:
            identifier = body.get("two_factor_info", {}).get("two_factor_identifier")
            if isinstance(identifier, str) and identifier:
                self._store_identifier(username, identifier)
                return InstagramLoginResult(
                    InstagramConnectStage.NEED_2FA, LoginFailureCategory.CHALLENGE_REQUIRED
                )
            return self._denied(LoginFailureCategory.CHALLENGE_REQUIRED)
        if self._two_factor_block(body):
            return InstagramLoginResult(
                InstagramConnectStage.NEED_2FA, LoginFailureCategory.CHALLENGE_REQUIRED
            )
        message = str(body.get("message") or "").lower()
        if "password" in message or "incorrect" in message or "username" in message:
            return self._denied(LoginFailureCategory.WRONG_CREDENTIALS)
        if "wait a few minutes" in message or "too many" in message:
            return self._denied(LoginFailureCategory.NOT_AVAILABLE)
        # Unknown/undocumented response shape: fail closed.
        return self._denied(LoginFailureCategory.SESSION_REJECTED)

    # -- helpers -----------------------------------------------------------------

    def _denied(self, category: LoginFailureCategory) -> InstagramLoginResult:
        return InstagramLoginResult(InstagramConnectStage.DENIED, category)

    def _store_identifier(self, username: str, identifier: str) -> None:
        now = time.time()
        self._identifiers = {
            u: (ts, i)
            for u, (ts, i) in self._identifiers.items()
            if ts > now - _TRANSIENT_TTL.total_seconds()
        }
        if len(self._identifiers) >= _MAX_TRANSIENT_IDENTIFIERS:
            oldest = min(self._identifiers, key=lambda u: self._identifiers[u][0])
            del self._identifiers[oldest]
        self._identifiers[username] = (now, identifier)

    def _complete_checkpoint(self, username: str, code: str) -> InstagramLoginResult:
        entry = self._identifiers.get(username)
        if not entry:
            # No pending two-factor challenge for this username: fail closed.
            return self._denied(LoginFailureCategory.CHALLENGE_REQUIRED)
        stored_at, identifier = entry
        if time.time() - stored_at > _TRANSIENT_TTL.total_seconds():
            del self._identifiers[username]
            return self._denied(LoginFailureCategory.CHALLENGE_REQUIRED)
        try:
            self._identifiers = {
                u: (ts, i) for u, (ts, i) in self._identifiers.items() if u != username
            }
            csrf = self._fetch_new_csrf()
            response = self._requester.request(
                "POST",
                _TWO_FACTOR_AJAX,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "X-CSRFToken": csrf,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": _LOGIN_PAGE,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                form={
                    "two_factor_identifier": identifier,
                    "username": username,
                    "verification_code": code,
                    "trust_this_device": "on",
                },
                timeout_seconds=self._timeout,
            )
        except ProviderHttpRequestError:
            return self._denied(LoginFailureCategory.NOT_AVAILABLE)
        try:
            body = response.json()
        except ValueError:
            return self._denied(LoginFailureCategory.SESSION_REJECTED)
        if not isinstance(body, dict) or response.status >= 400:
            return self._denied(LoginFailureCategory.NOT_AVAILABLE)
        if body.get("status") != "ok" or body.get("authenticated") is not True:
            if self._two_factor_block(body):
                return self._denied(LoginFailureCategory.CHALLENGE_REQUIRED)
            return self._denied(LoginFailureCategory.WRONG_CREDENTIALS)
        return self._finalize(username, response.headers)

    def _fetch_new_csrf(self) -> str:
        page = self._requester.request(
            "GET", _LOGIN_PAGE, headers={"User-Agent": "Mozilla/5.0"}, timeout_seconds=self._timeout
        )
        return _cookie_value(page.headers, "csrftoken") or ""

    def _finalize(self, username: str, headers: Mapping[str, str]) -> InstagramLoginResult:
        sessionid = _cookie_value(headers, "sessionid")
        if not sessionid:
            # Authenticated but no session cookie material: fail closed, never invent a session.
            return self._denied(LoginFailureCategory.SESSION_REJECTED)
        cookie_lines = ["# Netscape HTTP Cookie File"]
        for name, required in (
            ("sessionid", True),
            ("ds_user_id", True),
            ("rur", True),
            ("csrftoken", False),
        ):
            value = _cookie_value(headers, name)
            if required and not value:
                return self._denied(LoginFailureCategory.SESSION_REJECTED)
            if value:
                cookie_lines.append(f".instagram.com\tTRUE\t/\tTRUE\t0\t{name}\t{value}")
        for http_name, cookie_name in (("x-ig-set-www-claim", "ig_did"),):
            value = _cookie_value(headers, http_name)
            if value:
                cookie_lines.append(f".instagram.com\tTRUE\t/\tTRUE\t0\t{cookie_name}\t{value}")
        cookie_material = "\n".join(cookie_lines) + "\n"
        return InstagramLoginResult(
            InstagramConnectStage.CONNECTED,
            LoginFailureCategory.NONE,
            session_bytes=cookie_material.encode("utf-8"),
        )

    @staticmethod
    def _two_factor_block(body: Mapping[str, object]) -> bool:
        return body.get("two_factor_required") is True or (
            isinstance(body.get("error_type"), str) and "two_factor" in str(body.get("error_type"))
        )


def _jar(headers: Mapping[str, str]) -> str:
    pieces: list[str] = []
    for raw in headers.get("set-cookie", "").split(","):
        cleaned = raw.strip()
        if not cleaned:
            continue
        name_value = cleaned.split(";", 1)[0]
        if "=" in name_value:
            pieces.append(name_value)
    return "; ".join(pieces)
