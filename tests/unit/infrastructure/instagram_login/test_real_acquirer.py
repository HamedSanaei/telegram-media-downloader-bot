"""Real Instagram session acquirer tests (T025).

The shipped runtime must never fabricate a session: only a genuinely authenticated login flow
produces normalized Netscape cookie bytes, and any protocol/transport anomaly fails closed with a
typed denial.
"""

from __future__ import annotations

import json

from telegram_media_bot.domain.instagram_connection import (
    LoginFailureCategory,
)
from telegram_media_bot.domain.web_companion import InstagramConnectStage
from telegram_media_bot.infrastructure.instagram_login.real import RealInstagramSessionAcquirer
from telegram_media_bot.infrastructure.payments.base import (
    ProviderHttpRequestError,
    ProviderHttpResponse,
)


class ScriptedInstagram:
    """Bounded fake transport mimicking Instagram's login endpoints."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.handlers: dict[str, ProviderHttpResponse] = {}

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: object | None = None,
        form: object | None = None,
        json_body: object | None = None,
        timeout_seconds: float,
    ) -> ProviderHttpResponse:
        del headers, form, json_body, timeout_seconds
        key = f"{method} {url}"
        self.calls.append(key)
        for fragment, response in self.handlers.items():
            if fragment in key:
                return response
        raise AssertionError(f"unscripted: {key}")


def _page(csrf: str = "csrf-abc") -> ProviderHttpResponse:
    return ProviderHttpResponse(
        200, {"set-cookie": f"csrftoken={csrf}; Path=/; HttpOnly"}, b"<html/>"
    )


def _ajax_result(payload: dict[str, object], cookies: str = "") -> ProviderHttpResponse:
    return ProviderHttpResponse(200, {"set-cookie": cookies}, json.dumps(payload).encode())


def test_full_login_produces_netscape_session_material() -> None:
    transport = ScriptedInstagram()
    transport.handlers["GET https://www.instagram.com/accounts/login/"] = _page()
    transport.handlers["POST https://www.instagram.com/accounts/login/ajax/"] = _ajax_result(
        {"authenticated": True},
        "sessionid=ig-session; Path=/, ds_user_id=987654; Path=/, rur=FRC; Path=/",
    )
    acquirer = RealInstagramSessionAcquirer(requester=transport, timeout_seconds=5.0)

    result = acquirer.step(username="realuser", password="secret-pass", twofa_code=None)

    assert result.stage is InstagramConnectStage.CONNECTED
    assert result.failure is LoginFailureCategory.NONE
    assert result.session_bytes is not None
    material = result.session_bytes.decode("utf-8")
    assert material.startswith("# Netscape HTTP Cookie File")
    assert "sessionid\tig-session" in material
    assert "ds_user_id\t987654" in material
    assert "rur\tFRC" in material
    assert "secret-pass" not in material  # credentials never leak into session material


def test_login_requires_identity_and_password() -> None:
    transport = ScriptedInstagram()
    acquirer = RealInstagramSessionAcquirer(requester=transport, timeout_seconds=5.0)
    missing = acquirer.step(username=None, password=None, twofa_code=None)
    assert missing.stage is InstagramConnectStage.DENIED
    assert missing.failure is LoginFailureCategory.WRONG_CREDENTIALS


def test_two_factor_flow_then_code_completes() -> None:
    transport = ScriptedInstagram()
    transport.handlers["GET https://www.instagram.com/accounts/login/"] = _page("csrf-2fa")
    transport.handlers["POST https://www.instagram.com/accounts/login/ajax/"] = _ajax_result(
        {"two_factor_required": True, "two_factor_info": {"two_factor_identifier": "id-1"}}
    )
    transport.handlers["POST https://www.instagram.com/accounts/ajax/two_factor_ajax/"] = (
        _ajax_result(
            {"status": "ok", "authenticated": True},
            "sessionid=ig-twofa; Path=/, ds_user_id=42; Path=/, rur=R; Path=/",
        )
    )
    acquirer = RealInstagramSessionAcquirer(requester=transport, timeout_seconds=5.0)

    first = acquirer.step(username="realuser", password="secret-pass", twofa_code=None)
    assert first.stage is InstagramConnectStage.NEED_2FA
    assert first.failure is LoginFailureCategory.CHALLENGE_REQUIRED
    assert first.session_bytes is None

    second = acquirer.step(username="realuser", password=None, twofa_code="123456")
    assert second.stage is InstagramConnectStage.CONNECTED
    assert second.session_bytes is not None
    assert b"ig-twofa" in second.session_bytes


def test_two_factor_code_without_pending_challenge_fails_closed() -> None:
    transport = ScriptedInstagram()
    acquirer = RealInstagramSessionAcquirer(requester=transport, timeout_seconds=5.0)
    result = acquirer.step(username="nobody", password=None, twofa_code="123456")
    assert result.stage is InstagramConnectStage.DENIED
    assert result.failure is LoginFailureCategory.CHALLENGE_REQUIRED
    assert result.session_bytes is None


def test_wrong_password_is_typed_denial() -> None:
    transport = ScriptedInstagram()
    transport.handlers["GET https://www.instagram.com/accounts/login/"] = _page()
    transport.handlers["POST https://www.instagram.com/accounts/login/ajax/"] = _ajax_result(
        {"message": "The password you entered is incorrect"}
    )
    acquirer = RealInstagramSessionAcquirer(requester=transport, timeout_seconds=5.0)
    result = acquirer.step(username="realuser", password="wrong", twofa_code=None)
    assert result.stage is InstagramConnectStage.DENIED
    assert result.failure is LoginFailureCategory.WRONG_CREDENTIALS
    assert result.session_bytes is None


def test_authenticated_without_session_cookie_fails_closed() -> None:
    transport = ScriptedInstagram()
    transport.handlers["GET https://www.instagram.com/accounts/login/"] = _page()
    transport.handlers["POST https://www.instagram.com/accounts/login/ajax/"] = _ajax_result(
        {"authenticated": True}  # no sessionid cookie material
    )
    acquirer = RealInstagramSessionAcquirer(requester=transport, timeout_seconds=5.0)
    result = acquirer.step(username="realuser", password="secret-pass", twofa_code=None)
    assert result.stage is InstagramConnectStage.DENIED
    assert result.failure is LoginFailureCategory.SESSION_REJECTED


def test_transport_failure_is_typed_denial() -> None:
    class BrokenTransport:
        def request(
            self,
            method: str,
            url: str,
            *,
            headers: object | None = None,
            form: object | None = None,
            json_body: object | None = None,
            timeout_seconds: float,
        ) -> ProviderHttpResponse:
            del method, url, headers, form, json_body, timeout_seconds
            raise ProviderHttpRequestError("timeout")

    acquirer = RealInstagramSessionAcquirer(requester=BrokenTransport(), timeout_seconds=5.0)
    result = acquirer.step(username="realuser", password="secret-pass", twofa_code=None)
    assert result.stage is InstagramConnectStage.DENIED
    assert result.failure is LoginFailureCategory.NOT_AVAILABLE


def test_malformed_response_fails_closed() -> None:
    transport = ScriptedInstagram()
    transport.handlers["GET https://www.instagram.com/accounts/login/"] = _page()
    transport.handlers["POST https://www.instagram.com/accounts/login/ajax/"] = (
        ProviderHttpResponse(200, {}, b"not-json")
    )
    acquirer = RealInstagramSessionAcquirer(requester=transport, timeout_seconds=5.0)
    result = acquirer.step(username="realuser", password="secret-pass", twofa_code=None)
    assert result.stage is InstagramConnectStage.DENIED
    assert result.session_bytes is None
