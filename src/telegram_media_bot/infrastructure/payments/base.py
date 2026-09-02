"""Bounded provider HTTP base with create/inquiry retry separation (T024).

One rule governs every adapter in this package: the invoice-creation mutation is executed at most
once per order (the durable reservation lives in ``payment_creation_reservations``), so creation
requests never retry. Only read-only inquiries retry transient transport/HTTP failures (429, 5xx,
timeout, disconnect), within the operator-configured attempt count.

Responses are returned as bounded ``ProviderHttpResponse`` values; raw bodies are never logged and
the request layer never exposes provider credentials.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

_TRANSIENT_HTTP_STATUSES = frozenset(set(range(408, 430)) | set(range(500, 600)))


@dataclass(frozen=True, slots=True)
class ProviderHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> object:
        """Decode the body as JSON; raises ``json.JSONDecodeError`` on malformed input."""
        return json.loads(self.body.decode("utf-8"))


class ProviderHttpRequestError(Exception):
    """Transport-level failure (timeout, DNS, disconnect) with a stable category."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class ProviderHttpRequester(Protocol):
    """Synchronous, injectable HTTP transport for provider adapters (no event loop)."""

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
        """Perform one HTTP request and return the bounded response."""


def is_transient_status(status: int) -> bool:
    return status in _TRANSIENT_HTTP_STATUSES


class StdlibHttpRequester:
    """Production transport built on ``urllib.request`` with strict timeouts.

    No proxy/credential handling beyond what the caller's headers supply; the provider token is
    set by the adapter and never appears in the URL or in exceptions.
    """

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
        data: bytes | None = None
        request_headers: dict[str, str] = dict(headers or {})
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif json_body is not None:
            data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read()
                return ProviderHttpResponse(
                    status=int(response.status),
                    headers={str(key): str(value) for key, value in response.headers.items()},
                    body=payload,
                )
        except urllib.error.HTTPError as error:
            payload = error.read()
            return ProviderHttpResponse(
                status=int(error.code),
                headers={str(key): str(value) for key, value in error.headers.items()},
                body=payload,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            del exc
            raise ProviderHttpRequestError("provider_transport_failure") from None


def request_with_retries(
    requester: ProviderHttpRequester,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    form: Mapping[str, str] | None = None,
    json_body: object | None = None,
    timeout_seconds: float,
    retry_count: int,
) -> ProviderHttpResponse:
    """Read-only inquiry call with bounded retries on 429/5xx/transport failures.

    Retriable failures are retried at most ``retry_count`` times; a definitive failure or a
    successful response returns immediately. An exhausted budget raises
    ``ProviderHttpRequestError`` so callers classify the inquiry as unknown/pending.
    """
    attempt = 0
    while True:
        try:
            response = requester.request(
                method,
                url,
                headers=headers,
                form=form,
                json_body=json_body,
                timeout_seconds=timeout_seconds,
            )
        except ProviderHttpRequestError:
            attempt += 1
            if attempt > retry_count:
                raise
            continue
        if is_transient_status(response.status) and attempt < retry_count:
            attempt += 1
            continue
        return response


__all__ = [
    "ProviderHttpRequestError",
    "ProviderHttpRequester",
    "ProviderHttpResponse",
    "StdlibHttpRequester",
    "is_transient_status",
    "request_with_retries",
]
