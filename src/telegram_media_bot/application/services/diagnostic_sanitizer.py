"""Central diagnostic sanitizer for administrator-visible failure content.

Every failure payload that can reach structured logs, database persistence, or Telegram
administrator notifications must pass through this module first. URLs are reduced to
``scheme + hostname + safe path`` with query parameters stripped unless a parameter has been
explicitly classified safe, and exception messages are scanned for secret-shaped fragments.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

#: Query parameters that carry routing meaning only and are safe to keep in sanitized URLs.
SAFE_QUERY_PARAMETERS = frozenset(
    {
        "v",
        "t",
        "list",
        "index",
        "start_radio",
        "playnext",
        "pp",
        "si",
        "feature",
    }
)

#: Query parameter names whose values are always treated as secrets.
_SECRET_QUERY_PARAMETERS = frozenset(
    {
        "token",
        "access_token",
        "auth_token",
        "api_key",
        "apikey",
        "key",
        "secret",
        "password",
        "passwd",
        "pwd",
        "sig",
        "signature",
        "session",
        "sessionid",
        "session_id",
        "code",
        "state",
        "hash",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "x-goog-signature",
        "x-goog-credential",
        "x-goog-security-token",
    }
)

_SECRET_HEADER_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(x-api-key\s*[:=]\s*)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(api[_-]?hash\s*[:=]\s*)[A-Za-z0-9]{8,}"),
    re.compile(r"(?i)(api[_-]?id\s*[:=]\s*)[A-Za-z0-9]{6,}"),
    re.compile(r"(?i)(bot[\s_-]?token\s*[:=]\s*)\d+:[A-Za-z0-9_-]{20,}"),
)

_COOKIE_VALUE_PATTERN = re.compile(
    r"(?i)(sessionid|session|sapisid|ssid|apisid|auth_token|sid|csrftoken|ds_user_id|"
    r"ig_did|mid|rur|datr|dpr|xs|ct0|auth|twid|guest_id|personalization_id|"
    r"_ga|_gid|NID|SID|HSID|LOGIN_INFO|PREF|__Secure-3PSID|__Secure-3PAPISID)\s*=\s*"
    r"[A-Za-z0-9_%+.\-]{6,}"
)

_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(token|access_token|auth_token|api_key|apikey|api-id|api_id|api-hash|api_hash|"
    r"secret|password|passwd|pwd|session_id|sessionid|authorization|signature|sig)\b"
    r"\s*[:=]\s*[A-Za-z0-9._~+/=\-]{6,}"
)

_LONG_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_\-]{24,}")
_URL_CREDENTIALS_PATTERN = re.compile(r"(?i)(//[^/\s:@]+):([^/\s@]+)@")
_PROXY_PASSWORD_PATTERN = re.compile(r"(?i)(proxy\s*[:=]\s*[a-z]+://[^:/\s]+:)[^@\s/]+")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")

_MAX_REASON_CHARACTERS = 512
_MAX_PATH_CHARACTERS = 180


def sanitize_url(url: str) -> str:
    """Return ``scheme://hostname/path`` with secrets and tracking query parameters removed.

    Query parameters are dropped unless explicitly classified safe. Credentials embedded in
    the URL are never reproduced.
    """
    candidate = (url or "").strip()
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return "<invalid-url>"
    scheme = (parsed.scheme or "").casefold()
    hostname = (parsed.hostname or "").casefold()
    if scheme not in {"http", "https"} or not hostname:
        return "<invalid-url>"
    safe_query = tuple(
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() in SAFE_QUERY_PARAMETERS
    )
    path = _truncate_path(parsed.path)
    rebuilt = urlunsplit((scheme, hostname, path, "", ""))
    if safe_query:
        try:
            from urllib.parse import urlencode

            rebuilt = urlunsplit((scheme, hostname, path, urlencode(safe_query, doseq=True), ""))
        except Exception:
            rebuilt = urlunsplit((scheme, hostname, path, "", ""))
    return rebuilt


def sanitize_exception_message(message: str) -> str | None:
    """Sanitize and bound an exception message, or return ``None`` when it is empty."""
    if not message:
        return None
    cleaned = _CONTROL_CHARACTERS.sub(" ", message)
    cleaned = redact_secrets(cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    if not cleaned:
        return None
    return cleaned[:_MAX_REASON_CHARACTERS]


def redact_secrets(text: str) -> str:
    """Replace every secret-shaped fragment in ``text`` with a stable placeholder."""
    if not text:
        return text
    redacted = _URL_CREDENTIALS_PATTERN.sub(r"\1:****@", text)
    redacted = _PROXY_PASSWORD_PATTERN.sub(r"\1****", redacted)
    for pattern in _SECRET_HEADER_PATTERNS:
        redacted = pattern.sub(_header_replacement, redacted)
    redacted = _COOKIE_VALUE_PATTERN.sub(r"\1=***", redacted)
    redacted = _SECRET_ASSIGNMENT_PATTERN.sub(r"\1=<redacted>", redacted)
    redacted = _redact_secret_query_strings(redacted)
    redacted = _LONG_TOKEN_PATTERN.sub("<redacted>", redacted)
    return redacted


def _header_replacement(match: re.Match[str]) -> str:
    return f"{match.group(1)}<redacted>"


def _redact_secret_query_strings(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        query = match.group(0)
        safe_parts: list[str] = []
        for key, value in parse_qsl(query.lstrip("?"), keep_blank_values=True):
            if key.casefold() in SAFE_QUERY_PARAMETERS:
                safe_parts.append(f"{key}={value}")
            else:
                safe_parts.append(f"{key}=<redacted>")
        return f"?{'&'.join(safe_parts)}" if safe_parts else query

    return re.sub(r"\?[^\s\"']{0,400}", replace, text)


def _truncate_path(path: str) -> str:
    if len(path) <= _MAX_PATH_CHARACTERS:
        return path
    return path[:_MAX_PATH_CHARACTERS]
