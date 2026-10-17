"""Per-browser HTTPS cookie jar; bounded responses and same-origin login redirects only."""

from __future__ import annotations

import json
from collections.abc import Mapping
from http.client import HTTPMessage
from http.cookiejar import CookieJar
from typing import IO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from telegram_media_bot.infrastructure.payments.base import (
    ProviderHttpRequester,
    ProviderHttpRequestError,
    ProviderHttpResponse,
)

_MAX_BODY = 1024 * 1024


def _require_instagram_origin(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ProviderHttpRequestError("login_origin_rejected") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.instagram.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ProviderHttpRequestError("login_origin_rejected")


class _LoginRedirects(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: IO[bytes], code: int, msg: str, headers: HTTPMessage, newurl: str
    ) -> Request | None:
        _require_instagram_origin(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class InstagramLoginRequester(ProviderHttpRequester):
    """One instance per interactive attempt, never shared between Telegram owners."""

    def __init__(self) -> None:
        self._jar = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._jar), _LoginRedirects())

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        form: Mapping[str, str] | None = None,
        json_body: object | None = None,
        timeout_seconds: float,
    ) -> ProviderHttpResponse:
        _require_instagram_origin(url)
        data = urlencode(form).encode("utf-8") if form is not None else None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
        request = Request(url, data=data, headers=dict(headers or {}), method=method)
        try:
            try:
                response = self._opener.open(request, timeout=timeout_seconds)
            except HTTPError as error:
                response = error
            with response:
                body = response.read(_MAX_BODY + 1)
                if len(body) > _MAX_BODY:
                    raise ProviderHttpRequestError("login_response_too_large")
                result_headers = {
                    str(key).lower(): str(value) for key, value in response.headers.items()
                }
                # CookieJar processes all Set-Cookie headers, including Expires commas. Return
                # the cumulative jar so final login cookies may span GET, password and 2FA.
                cookies: list[str] = []
                for cookie in self._jar:
                    if (
                        cookie.domain.lstrip(".") in {"instagram.com", "www.instagram.com"}
                        and cookie.value is not None
                    ):
                        cookies.append(f"{cookie.name}={cookie.value}")
                result_headers["set-cookie"] = "\n".join(cookies)
                return ProviderHttpResponse(int(response.code), result_headers, body)
        except URLError, TimeoutError, OSError, ValueError:
            raise ProviderHttpRequestError("login_transport_failure") from None
