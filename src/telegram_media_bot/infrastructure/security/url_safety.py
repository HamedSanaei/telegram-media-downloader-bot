from __future__ import annotations

import ipaddress
import socket
from urllib.parse import SplitResult, urlsplit, urlunsplit

from telegram_media_bot.application.ports.url_validator import UrlValidator
from telegram_media_bot.domain.errors import InvalidUrlError, UnsafeUrlError

_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.google",
}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".lan")


class PublicUrlValidator(UrlValidator):
    def __init__(self, *, reject_private_networks: bool = True) -> None:
        self._reject_private_networks = reject_private_networks

    def validate(self, url: str) -> str:
        candidate = url.strip()
        parsed = urlsplit(candidate)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            raise InvalidUrlError("Only absolute HTTP(S) URLs are accepted")
        if parsed.username or parsed.password:
            raise InvalidUrlError("Credentials in URLs are not accepted")
        try:
            port = parsed.port
        except ValueError as exc:
            raise InvalidUrlError("URL port is invalid") from exc
        try:
            hostname = parsed.hostname.encode("idna").decode("ascii").casefold().rstrip(".")
        except UnicodeError as exc:
            raise InvalidUrlError("URL hostname is invalid") from exc
        if not hostname or hostname in _BLOCKED_HOSTNAMES or hostname.endswith(_BLOCKED_SUFFIXES):
            raise UnsafeUrlError("Local or internal hostnames are not accepted")
        if self._reject_private_networks:
            self._validate_addresses(hostname, port)
        normalized = SplitResult(
            scheme=parsed.scheme.casefold(),
            netloc=_netloc(hostname, port),
            path=parsed.path or "/",
            query=parsed.query,
            fragment="",
        )
        return urlunsplit(normalized)

    @staticmethod
    def _validate_addresses(hostname: str, port: int | None) -> None:
        try:
            literal = ipaddress.ip_address(hostname.strip("[]"))
            addresses = {literal}
        except ValueError:
            try:
                records = socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)
            except socket.gaierror as exc:
                raise InvalidUrlError("URL hostname could not be resolved") from exc
            addresses = {ipaddress.ip_address(record[4][0]) for record in records}
        if not addresses:
            raise InvalidUrlError("URL hostname did not resolve")
        if any(not _is_globally_routable(address) for address in addresses):
            raise UnsafeUrlError("URL resolves to a non-public network")


def _is_globally_routable(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address.is_global and not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _netloc(hostname: str, port: int | None) -> str:
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{display_host}:{port}" if port is not None else display_host
