from __future__ import annotations

from collections.abc import Sequence
from time import monotonic
from typing import Protocol

import structlog

from telegram_media_bot.application.ports.cookie_health import ActiveCookieProbe
from telegram_media_bot.application.services.diagnostic_sanitizer import (
    sanitize_exception_message,
    sanitize_url,
)
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.cookie_health import ActiveProbeResult, CookieHealthState
from telegram_media_bot.domain.cookies import CookieService
from telegram_media_bot.domain.errors import (
    GalleryDlAuthenticationRequiredError,
    GalleryDlCookiesExpiredError,
    RateLimitedError,
)
from telegram_media_bot.infrastructure.gallerydl.command_builder import GalleryDlCommandBuilder
from telegram_media_bot.infrastructure.gallerydl.errors import map_process_failure
from telegram_media_bot.infrastructure.gallerydl.models import GalleryProcessResult
from telegram_media_bot.infrastructure.gallerydl.runner import GalleryDlRunner

logger = structlog.get_logger(__name__)


class ProbeRunner(Protocol):
    async def run_async(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> GalleryProcessResult: ...


class GalleryDlCookieProbe(ActiveCookieProbe):
    """Lightweight authenticated probe built on gallery-dl inspection.

    Only providers with an explicitly configured authentication-required probe URL are
    probed; everything else stays UNVERIFIED. A probe never downloads media (simulate mode)
    and uses a short bounded timeout so Telegram callbacks are never blocked.
    """

    def __init__(self, settings: Settings, runner: ProbeRunner | None = None) -> None:
        self._settings = settings
        self._runner = runner or GalleryDlRunner()
        self._commands = GalleryDlCommandBuilder(
            settings.gallery_dl,
            settings.effective_cookie_file(),
        )

    async def probe(self, provider: CookieService) -> ActiveProbeResult:
        spec = self._settings.cookie_health.probes.get(provider.value)
        if spec is None or not spec.url:
            return ActiveProbeResult(
                provider=provider,
                status=CookieHealthState.UNVERIFIED,
                safe_reason="no authenticated probe configured for this provider",
            )
        started = monotonic()
        sanitized_url = sanitize_url(spec.url)
        try:
            args = self._commands.inspect_url(provider.value, spec.url)
            result = await self._runner.run_async(
                args,
                timeout_seconds=self._settings.cookie_health.probe_timeout_seconds,
            )
        except Exception as exc:
            safe_reason = sanitize_exception_message(str(exc)) or "probe failed"
            await logger.awarning(
                "cookie_probe_raised",
                provider=provider.value,
                probed_url=sanitized_url,
                error_type=type(exc).__name__,
                elapsed_seconds=round(monotonic() - started, 3),
            )
            return ActiveProbeResult(
                provider=provider,
                status=CookieHealthState.CHECK_ERROR,
                probed_url=sanitized_url,
                auth_required_endpoint=spec.auth_required,
                elapsed_seconds=round(monotonic() - started, 3),
                safe_reason=safe_reason,
            )
        elapsed = round(monotonic() - started, 3)
        if result.return_code != 0:
            error = map_process_failure(result.return_code, result.stderr)
            if isinstance(
                error, (GalleryDlCookiesExpiredError, GalleryDlAuthenticationRequiredError)
            ):
                await logger.awarning(
                    "cookie_probe_auth_failed",
                    provider=provider.value,
                    probed_url=sanitized_url,
                    error_type=type(error).__name__,
                    http_status=getattr(error, "http_status", None),
                    elapsed_seconds=elapsed,
                )
                return ActiveProbeResult(
                    provider=provider,
                    status=CookieHealthState.AUTH_FAILED,
                    probed_url=sanitized_url,
                    auth_required_endpoint=spec.auth_required,
                    http_status=getattr(error, "http_status", None),
                    elapsed_seconds=elapsed,
                    safe_reason=sanitize_exception_message(str(error)) or "authentication failed",
                )
            if isinstance(error, RateLimitedError):
                return ActiveProbeResult(
                    provider=provider,
                    status=CookieHealthState.CHECK_ERROR,
                    probed_url=sanitized_url,
                    auth_required_endpoint=spec.auth_required,
                    http_status=getattr(error, "http_status", None),
                    elapsed_seconds=elapsed,
                    safe_reason="provider rate limited the probe",
                )
            await logger.awarning(
                "cookie_probe_failed",
                provider=provider.value,
                probed_url=sanitized_url,
                error_type=type(error).__name__,
                elapsed_seconds=elapsed,
            )
            return ActiveProbeResult(
                provider=provider,
                status=CookieHealthState.CHECK_ERROR,
                probed_url=sanitized_url,
                auth_required_endpoint=spec.auth_required,
                http_status=getattr(error, "http_status", None),
                elapsed_seconds=elapsed,
                safe_reason=sanitize_exception_message(str(error)) or "probe failed",
            )
        if _has_jsonl_events(result.stdout):
            if not spec.auth_required:
                return ActiveProbeResult(
                    provider=provider,
                    status=CookieHealthState.UNVERIFIED,
                    probed_url=sanitized_url,
                    auth_required_endpoint=False,
                    elapsed_seconds=elapsed,
                    safe_reason=(
                        "probe endpoint does not require authentication; "
                        "anonymous success cannot prove cookie health"
                    ),
                )
            return ActiveProbeResult(
                provider=provider,
                status=CookieHealthState.HEALTHY,
                probed_url=sanitized_url,
                auth_required_endpoint=True,
                elapsed_seconds=elapsed,
                safe_reason=None,
            )
        return ActiveProbeResult(
            provider=provider,
            status=CookieHealthState.UNVERIFIED,
            probed_url=sanitized_url,
            auth_required_endpoint=spec.auth_required,
            elapsed_seconds=elapsed,
            safe_reason="probe returned no events; authentication could not be verified",
        )


def _has_jsonl_events(payload: bytes) -> bool:
    text = payload.decode("utf-8", errors="replace").strip()
    return any(line.strip() for line in text.splitlines())
