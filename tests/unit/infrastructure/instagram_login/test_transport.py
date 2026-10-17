from http.client import HTTPMessage
from http.cookiejar import MozillaCookieJar
from io import BytesIO
from pathlib import Path
from urllib.request import BaseHandler, Request
from urllib.response import addinfourl

import pytest

from telegram_media_bot.domain.web_companion import InstagramConnectStage
from telegram_media_bot.infrastructure.instagram_login.real import (
    RealInstagramSessionAcquirer,
    _cookie_value,
)
from telegram_media_bot.infrastructure.instagram_login.transport import (
    InstagramLoginRequester,
    _LoginRedirects,
)
from telegram_media_bot.infrastructure.payments.base import ProviderHttpRequestError


class FixtureResponse(addinfourl):
    msg = "OK"


class FixtureHTTPS(BaseHandler):
    handler_order = 100

    def __init__(self, *, oversized: bool = False) -> None:
        self.calls: list[Request] = []
        self.oversized = oversized

    def https_open(self, request: Request) -> addinfourl:
        self.calls.append(request)
        headers = HTTPMessage()
        if len(self.calls) == 1:
            headers.add_header(
                "Set-Cookie",
                "csrftoken=csrf-fixture; Path=/; Expires=Wed, 31 Dec 2030 23:59:59 GMT; Secure",
            )
            headers.add_header("Set-Cookie", "mid=mid-fixture; Path=/; Secure")
        else:
            headers.add_header("Set-Cookie", 'rur="FRC\\054123"; Path=/; Secure')
            headers.add_header("Set-Cookie", "sessionid=session-fixture; Path=/; Secure")
            headers.add_header("Set-Cookie", "ds_user_id=42; Path=/; Secure")
        result = FixtureResponse(
            BytesIO(b"x" * (1024 * 1024 + 1) if self.oversized else b'{"authenticated":true}'),
            headers,
            request.full_url,
            200,
        )
        return result


def test_transport_retains_all_cookies_and_never_reuses_another_attempt() -> None:
    transport = InstagramLoginRequester()
    handler = FixtureHTTPS()
    transport._opener.add_handler(handler)
    page = transport.request("GET", "https://www.instagram.com/accounts/login/", timeout_seconds=1)
    assert _cookie_value(page.headers, "csrftoken") == "csrf-fixture"
    assert _cookie_value(page.headers, "mid") == "mid-fixture"
    final = transport.request(
        "POST",
        "https://www.instagram.com/accounts/login/ajax/",
        form={"username": "fixture"},
        timeout_seconds=1,
    )
    assert _cookie_value(final.headers, "csrftoken") == "csrf-fixture"
    assert _cookie_value(final.headers, "rur") == "FRC,123"
    assert "mid=mid-fixture" in (handler.calls[1].get_header("Cookie") or "")
    assert list(InstagramLoginRequester()._jar) == []


@pytest.mark.parametrize(
    "url",
    [
        "http://www.instagram.com/",
        "https://evil.example/",
        "https://www.instagram.com.evil.example/",
        "https://user:pass@www.instagram.com/",  # pragma: allowlist secret - rejected URL fixture
        "https://www.instagram.com:bad/",
    ],
)
def test_transport_rejects_foreign_origins_and_redirects(url: str) -> None:
    with pytest.raises(ProviderHttpRequestError):
        InstagramLoginRequester().request("GET", url, timeout_seconds=1)
    with pytest.raises(ProviderHttpRequestError):
        _LoginRedirects().redirect_request(
            Request("https://www.instagram.com/"), BytesIO(), 302, "redirect", HTTPMessage(), url
        )


def test_transport_bounds_response_bodies() -> None:
    transport = InstagramLoginRequester()
    transport._opener.add_handler(FixtureHTTPS(oversized=True))
    with pytest.raises(ProviderHttpRequestError, match="too_large"):
        transport.request("GET", "https://www.instagram.com/", timeout_seconds=1)


def test_real_transport_session_preserves_quoted_cookie_on_materialization(tmp_path: Path) -> None:
    transport = InstagramLoginRequester()
    transport._opener.add_handler(FixtureHTTPS())
    result = RealInstagramSessionAcquirer(requester=transport).step(
        username="fixture",
        password="fixture-password",  # pragma: allowlist secret - synthetic transport
        twofa_code=None,
    )
    assert result.stage is InstagramConnectStage.CONNECTED and result.session_bytes is not None
    path = tmp_path / "cookies.txt"
    path.write_bytes(result.session_bytes)
    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    # Both media-engine loaders interpret Netscape expiry 0 as a session cookie;
    # the standard library alone treats it as an expired Unix timestamp.
    for cookie in jar:
        assert cookie.expires == 0
        cookie.expires = None
    request = Request("https://www.instagram.com/p/fixture/")
    jar.add_cookie_header(request)
    assert 'rur="FRC\\054123"' in (request.get_header("Cookie") or "")
    assert b"session-fixture" not in repr(result).encode()
