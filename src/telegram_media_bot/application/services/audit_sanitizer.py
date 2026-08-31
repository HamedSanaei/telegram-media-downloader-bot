"""Central redaction boundary for Operator Logger human-readable content."""

from __future__ import annotations

import re

from telegram_media_bot.domain.errors import MediaBotError


class UnsafeAuditPayloadError(MediaBotError):
    pass


_TRACEBACK = re.compile(r"(?i)traceback \(most recent call last\)|\n\s*file \"[^\"]+\"")
_PATH = re.compile(r"(?i)(?:[a-z]:\\(?:users|windows|programdata)\\|/home/|/data/|/run/secrets/)")
_NETSCAPE_ROW = re.compile(
    r"(?m)^\.?[^\t\r\n]+\t(?:TRUE|FALSE)\t/[^\t]*\t(?:TRUE|FALSE)\t\d+\t[^\t]+\t[^\t\r\n]+\r?$"
)
_BOT_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_SECRET_HEADER = re.compile(
    r"(?im)\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*[^\r\n]+"
)
_PROXY_URI = re.compile(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?P<key>[\"']?(?:"
    r"bot[_ -]?token|instagram[_ -]?(?:password|session|2fa|checkpoint)|"
    r"password|2fa|checkpoint|cookie|cookies|session|sessionid|csrftoken|"
    r"vault[_ -]?key|"
    r"payment[_ -]?(?:secret|signature|callback[_ -]?signature)|"
    r"callback[_ -]?signature|provider[_ -]?transaction[_ -]?reference|"
    r"provider[_ -]?secret|gateway[_ -]?secret|signed[_ -]?login[_ -]?token|"
    r"card[_ -]?(?:number|secret)|proxy[_ -]?(?:password|credentials?)|"
    r"authorization"
    r")[\"']?)\s*[:=]\s*(?P<value>[^,;\r\n]+)"
)


def sanitize_audit_message(value: object) -> str:
    """Redact actual secret-shaped values while preserving ordinary operational prose."""
    if not isinstance(value, str):
        raise UnsafeAuditPayloadError("audit messages must be pre-rendered strings")
    if _TRACEBACK.search(value) or _PATH.search(value) or _NETSCAPE_ROW.search(value):
        raise UnsafeAuditPayloadError("unsafe structured audit payload rejected")
    text = _BOT_TOKEN.sub("<redacted-bot-token>", value)
    text = _SECRET_HEADER.sub(lambda match: f"{match.group(0).split(':', 1)[0]}=<redacted>", text)
    text = _PROXY_URI.sub(r"\1<redacted>@", text)
    text = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group('key').strip(chr(34) + chr(39))}=<redacted>", text
    )
    text = " ".join(text.strip().split())
    if not text:
        raise UnsafeAuditPayloadError("empty audit payload rejected")
    return text[:2000]


def safe_failure_class(exc: BaseException) -> str:
    """Return only a stable class name, never exception text or repr."""
    return type(exc).__name__[:96]


__all__ = ["UnsafeAuditPayloadError", "safe_failure_class", "sanitize_audit_message"]
