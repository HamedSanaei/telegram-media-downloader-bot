"""Explicit recoverability model for terminal job failures.

Recovery is decided from a typed, centralized vocabulary — never from exception strings. A job is
recoverable only when BOTH the request is a supported provider request AND the terminal error
category maps to an explicit remediation class. Everything else stays terminal forever.
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlsplit

from telegram_media_bot.domain.cookies import CookieService, cookie_service_for_domain
from telegram_media_bot.domain.models import ErrorCategory, JobRecord


class RecoverabilityClass(StrEnum):
    #: Terminal — never automatically retried after a cookie update or an app fix.
    NONE = "none"
    #: Retry once the operator replaces/merges the provider's cookie.
    AFTER_COOKIE_CHANGE = "after_cookie_change"
    #: Retry once a newer application/app-fix version is deployed.
    AFTER_APP_FIX = "after_app_fix"


#: Category -> remediation class. Unlisted categories map to :attr:`RecoverabilityClass.NONE`.
_RECOVERABILITY_MAP: dict[ErrorCategory, RecoverabilityClass] = {
    ErrorCategory.AUTHENTICATION: RecoverabilityClass.AFTER_COOKIE_CHANGE,
    ErrorCategory.GALLERY_COOKIES_EXPIRED: RecoverabilityClass.AFTER_COOKIE_CHANGE,
    ErrorCategory.INTERNAL: RecoverabilityClass.AFTER_APP_FIX,
    ErrorCategory.LOCAL_RUNTIME: RecoverabilityClass.AFTER_APP_FIX,
    ErrorCategory.GALLERY_OUTPUT_CHANGED: RecoverabilityClass.AFTER_APP_FIX,
}


def recoverability_for_category(category: ErrorCategory) -> RecoverabilityClass:
    """Centralized, testable mapping from an error category to a remediation class."""
    return _RECOVERABILITY_MAP.get(category, RecoverabilityClass.NONE)


def provider_for_job(job: JobRecord) -> CookieService | None:
    """Supported cookie provider for a durable job, derived from its URL host.

    Returns ``None`` for unsupported providers (Pornhub, random sites, non-URL jobs), which makes
    recovery fail closed.
    """
    hostname = urlsplit(job.url).hostname
    if not hostname:
        return None
    return cookie_service_for_domain(hostname)


def recovery_class_for_job(job: JobRecord, category: ErrorCategory) -> RecoverabilityClass:
    """Effective recoverability for one terminal failure (supported request AND recoverable)."""
    if not provider_for_job(job):
        return RecoverabilityClass.NONE
    return recoverability_for_category(category)
