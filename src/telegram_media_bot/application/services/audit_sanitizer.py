"""Central redaction boundary for Operator Logger human-readable content."""

from __future__ import annotations

import re

from telegram_media_bot.domain.errors import MediaBotError


class UnsafeAuditPayloadError(MediaBotError):
    pass


_TRACEBACK = re.compile(r"(?i)traceback \(most recent call last\)|\n\s*file \"[^\"]+\"")
_PATH = re.compile(r"(?i)(?:[a-z]:\\(?:users|windows|programdata)\\|/home/|/data/|/run/secrets/)")
_NETSCAPE_ROW = re.compile(
    r"(?m)^\.?[^\t\r\n]+\t(?:TRUE|FALSE)\t/[^\t]*\t(?:TRUE|FALSE)\t\d+\t[^\t]+\t[^\t]+$"
)
_BOT_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_AUTHORIZATION = re.compile(r"(?i)(authorization\s*:\s*)(?:bearer\s+)?[^\s,;]+")
_PROXY_URI = re.compile(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b("
    r"bot[_ -]?token|instagram[_ -]?(?:password|session|2fa|checkpoint)|"
    r"password|2fa|checkpoint|cookie|cookies|session|vault[_ -]?key|"
    r"payment[_ -]?(?:secret|signature|callback[_ -]?signature)|"
    r"callback[_ -]?signature|provider[_ -]?transaction[_ -]?reference|"
    r"provider[_ -]?secret|proxy[_ -]?(?:password|credentials?)|"
    r"authorization"
    r")\s*[:=]\s*([^\s,;]+)"
)


def sanitize_audit_message(value: object) -> str:
    """Redact actual secret-shaped values while preserving ordinary operational prose."""
    if not isinstance(value, str):
        raise UnsafeAuditPayloadError("audit messages must be pre-rendered strings")
    if _TRACEBACK.search(value) or _PATH.search(value) or _NETSCAPE_ROW.search(value):
        raise UnsafeAuditPayloadError("unsafe structured audit payload rejected")
    text = _BOT_TOKEN.sub("<redacted-bot-token>", value)
    text = _AUTHORIZATION.sub(r"\1<redacted>", text)
    text = _PROXY_URI.sub(r"\1<redacted>@", text)
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = " ".join(text.strip().split())
    if not text:
        raise UnsafeAuditPayloadError("empty audit payload rejected")
    return text[:2000]


def safe_failure_class(exc: BaseException) -> str:
    """Return only a stable class name, never exception text or repr."""
    return type(exc).__name__[:96]


__all__ = ["UnsafeAuditPayloadError", "safe_failure_class", "sanitize_audit_message"]
