"""Separate least-privilege aiohttp.web companion boundary (T016).

Runs browser Instagram-connection routes and machine payment-callback routes in one optional
process with *independent* request handling, CSRF/session scope, and middleware so that no
browser state can ever confirm a payment and no callback can establish a browser session. The
process holds no Telegram bot token and no handoff signing key.

Security posture: Secure/HttpOnly/SameSite session cookies, synchronizer CSRF on browser
mutations, restrictive CSP/no-referrer/x-frame security headers, explicit trusted-proxy handling,
bounded request bodies/timeouts/rate limits, no permissive CORS, and sanitized handling that never
writes tokens, query strings, headers, body bytes, or callback payloads.
"""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from time import monotonic

from aiohttp import web

from telegram_media_bot.application.ports.companion import (
    InstagramConnectFlow,
    PaymentCallbackProcessor,
    ProviderCallbackRegistry,
)
from telegram_media_bot.domain.web_companion import (
    BrowserSession,
    HandoffClaim,
    HandoffPurpose,
    InstagramConnectResult,
    InstagramConnectStage,
    PaymentCallbackOutcome,
    new_browser_session_id,
    new_csrf_token,
    sha256_digest,
)

_SESSION_COOKIE = "tmb_web_session"
_CSRF_HEADER = "X-TMB-CSRF"
_GENERIC_SESSION_REJECTED = {"status": "session_invalid", "error": "SESSION_INVALID"}
_SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'; img-src 'self'; style-src 'self'; connect-src 'self'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class CompanionWebError(Exception):
    """Controlled web-boundary failure carrying a safe HTTP status and JSON payload."""

    def __init__(self, status: int, payload: Mapping[str, object]) -> None:
        super().__init__(str(status))
        self.status = status
        self.payload = payload


# Convenience type for the injected handoff-exchange callable (avoids importing the service).
HandoffExchangeCallable = Callable[[str, HandoffPurpose], object]


class BoundedSessionTable:
    """Bounded in-memory server-side browser sessions (digest -> session)."""

    def __init__(self, *, max_age_seconds: int, max_sessions: int) -> None:
        if max_age_seconds < 1 or max_sessions < 1:
            raise ValueError("session bounds must be positive")
        self.max_age_seconds = max_age_seconds
        self.max_sessions = max_sessions
        self._entries: dict[str, tuple[float, BrowserSession]] = {}

    def _now(self) -> float:
        return monotonic()

    def _current(self) -> datetime:
        return datetime.now(UTC)

    def _expire(self, now: float) -> None:
        current = self._current()
        stale = [
            key
            for key, (created, session) in self._entries.items()
            if now - created >= self.max_age_seconds or session.expires_at <= current
        ]
        for key in stale:
            self._entries.pop(key, None)

    def store(self, session: BrowserSession) -> None:
        now = self._now()
        self._expire(now)
        self._entries[sha256_digest(session.id)] = (now, session)
        if len(self._entries) > self.max_sessions:
            oldest = min(self._entries.items(), key=lambda item: item[1][0])
            del self._entries[oldest[0]]

    def lookup(self, raw_id: str) -> BrowserSession | None:
        entry = self._entries.get(sha256_digest(raw_id))
        if entry is None:
            return None
        session = entry[1]
        if monotonic() - entry[0] >= self.max_age_seconds or session.expires_at <= self._current():
            self._entries.pop(sha256_digest(raw_id), None)
            return None
        return session

    def drop(self, raw_id: str) -> None:
        self._entries.pop(sha256_digest(raw_id), None)

    @property
    def size(self) -> int:
        self._expire(monotonic())
        return len(self._entries)


class FixedWindowRateLimiter:
    """Bounded in-memory fixed-window rate limiter keyed by client IP (never a metric label)."""

    def __init__(self, *, limit: int, window_seconds: int) -> None:
        if window_seconds < 1:
            raise ValueError("rate-limit window must be positive")
        self._limit = limit
        self._window = window_seconds
        self._buckets: dict[str, tuple[int, float]] = {}

    def allow(self, ip: str) -> bool:
        now = monotonic()
        count, started = self._buckets.get(ip, (0, now))
        if now - started >= self._window:
            count = 0
            started = now
        count += 1
        self._buckets[ip] = (count, started)
        if len(self._buckets) > 4096:
            cutoff = now - self._window
            self._buckets = {k: v for k, v in self._buckets.items() if v[1] > cutoff}
        return count <= self._limit


class CompanionWebApp:
    """Builds and configures the companion ``aiohttp.web.Application``."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        session_max_seconds: int,
        interactive_flow_max_seconds: int,
        interactive_flow_max_sessions: int,
        body_limit_bytes: int,
        read_timeout_seconds: float,
        rate_limit_per_minute: int,
        trusted_proxies: tuple[str, ...],
        handoff_exchange: HandoffExchangeCallable,
        flow: InstagramConnectFlow,
        provider_registry: ProviderCallbackRegistry,
        payment_processor: PaymentCallbackProcessor,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._host = host
        self._port = port
        self._session_max_seconds = session_max_seconds
        self._body_limit = body_limit_bytes
        self._timeout_seconds = read_timeout_seconds
        self._handoff_exchange = handoff_exchange
        self._flow = flow
        self._registry = provider_registry
        self._payment_processor = payment_processor
        self._trusted_proxies = tuple(trusted_proxies)
        self._now = now
        self._sessions = BoundedSessionTable(
            max_age_seconds=max(session_max_seconds, interactive_flow_max_seconds),
            max_sessions=interactive_flow_max_sessions,
        )
        self._ratelimiter = FixedWindowRateLimiter(limit=rate_limit_per_minute, window_seconds=60)
        self._app: web.Application | None = None

    def build(self) -> web.Application:
        middlewares = [
            self._error_middleware,
            self._security_headers,
            self._timeout_middleware,
        ]
        app = web.Application(middlewares=middlewares)
        app.router.add_post("/instagram/connect/exchange", self._handle_exchange)
        app.router.add_post("/instagram/connect/complete", self._handle_flow_step)
        app.router.add_post("/payment/callback/{provider}", self._handle_payment_callback)
        app.router.add_get("/health", self._liveness)
        app.router.add_get("/ready", self._readiness)
        self._app = app
        return app

    # -- middleware --------------------------------------------------------

    @web.middleware
    async def _error_middleware(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        try:
            return await handler(request)
        except CompanionWebError as exc:
            return web.json_response(dict(exc.payload), status=exc.status)
        except web.HTTPException as exc:
            raise exc
        except Exception as exc:
            del exc
            return web.json_response({"status": "internal", "error": "INTERNAL"}, status=500)

    @web.middleware
    async def _security_headers(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        response = await handler(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    @web.middleware
    async def _timeout_middleware(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await handler(request)
        except TimeoutError as exc:
            raise web.HTTPRequestTimeout(text="") from exc

    def _real_client_ip(self, request: web.Request) -> str:
        address = request.transport.get_extra_info("peername") if request.transport else None
        ip = address[0] if address and len(address) >= 1 else "unknown"
        # Only a explicitly trusted reverse proxy may present a forwarded client address;
        # otherwise the peer address (already not user spoofable) is authoritative.
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded and self._trusted_proxies and ip in self._trusted_proxies:
            candidate = forwarded.split(",")[0].strip()
            if candidate:
                ip = candidate
        return ip

    async def _limit_body(self, request: web.Request) -> bytes:
        data = await request.content.read(self._body_limit + 1)
        if len(data) > self._body_limit:
            raise CompanionWebError(
                413, {"status": "payload_too_large", "error": "PAYLOAD_TOO_LARGE"}
            )
        return data

    # -- handlers ----------------------------------------------------------

    async def _handle_exchange(self, request: web.Request) -> web.Response:
        self._rate_limit(request)
        raw = await self._read_json_body(request)
        token = raw.get("token")
        if not isinstance(token, str) or not token:
            raise CompanionWebError(400, {"status": "bad_request", "error": "BAD_REQUEST"})
        result = self._handoff_exchange(token, HandoffPurpose.INSTAGRAM_CONNECT)
        if not _handoff_verified(result):
            raise CompanionWebError(401, _GENERIC_SESSION_REJECTED)
        claim = _handoff_claim(result)
        now = self._now()
        session = BrowserSession(
            id=new_browser_session_id(),
            csrf_token=new_csrf_token(),
            owner_user_id=claim.owner_user_id,
            purpose=HandoffPurpose.INSTAGRAM_CONNECT,
            created_at=now,
            expires_at=now + timedelta(seconds=self._session_max_seconds),
        )
        self._sessions.store(session)
        response = web.json_response({"status": "ok", "csrf_token": session.csrf_token})
        response.set_cookie(
            _SESSION_COOKIE,
            session.id,
            max_age=self._session_max_seconds,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    async def _handle_flow_step(self, request: web.Request) -> web.Response:
        self._rate_limit(request)
        session, input_value = await self._authorize_browser_session(request)
        result = await self._flow.step(
            owner_user_id=session.owner_user_id,
            session_id=session.id,
            input_value=input_value,
        )
        if not isinstance(result, InstagramConnectResult):
            raise CompanionWebError(500, {"status": "internal", "error": "INTERNAL"})
        payload: dict[str, object] = {"stage": result.stage.value}
        if result.message:
            payload["message"] = result.message[:512]
        if result.stage is InstagramConnectStage.DENIED:
            raise CompanionWebError(403, payload)
        if result.stage is InstagramConnectStage.NOT_AVAILABLE:
            raise CompanionWebError(404, payload)
        return web.json_response(payload)

    async def _handle_payment_callback(self, request: web.Request) -> web.Response:
        self._rate_limit(request)
        provider = str(request.match_info.get("provider", ""))[:128]
        body = await self._limit_body(request)
        verifier = self._registry.verifier_for(provider)
        if verifier is None:
            raise CompanionWebError(404, {"status": "not_found", "error": "NOT_FOUND"})
        if not verifier.verify_callback(body):
            raise CompanionWebError(403, {"status": "invalid", "error": "INVALID"})
        outcome = await self._payment_processor.process(provider_id=provider, provider_payload=body)
        if outcome is PaymentCallbackOutcome.ACCEPTED:
            return web.json_response({"status": "accepted"})
        if outcome is PaymentCallbackOutcome.REJECTED:
            raise CompanionWebError(400, {"status": "rejected", "error": "REJECTED"})
        raise CompanionWebError(404, {"status": "not_found", "error": "NOT_FOUND"})

    async def _liveness(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _readiness(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    # -- helpers -----------------------------------------------------------

    def _rate_limit(self, request: web.Request) -> None:
        if not self._ratelimiter.allow(self._real_client_ip(request)):
            raise CompanionWebError(429, {"status": "rate_limited", "error": "RATE_LIMITED"})

    async def _authorize_browser_session(
        self, request: web.Request
    ) -> tuple[BrowserSession, str | None]:
        cookie = request.cookies.get(_SESSION_COOKIE)
        if cookie is None:
            raise CompanionWebError(401, _GENERIC_SESSION_REJECTED)
        session = self._sessions.lookup(cookie)
        if session is None:
            raise CompanionWebError(401, _GENERIC_SESSION_REJECTED)
        sent = request.headers.get(_CSRF_HEADER, "")
        if not sent or not hmac.compare_digest(sent, session.csrf_token):
            raise CompanionWebError(403, {"status": "csrf_failed", "error": "CSRF_FAILED"})
        raw = await self._read_json_body(request)
        input_value = raw.get("input")
        if input_value is not None and (
            not isinstance(input_value, str) or len(input_value) > 4096
        ):
            raise CompanionWebError(
                413, {"status": "payload_too_large", "error": "PAYLOAD_TOO_LARGE"}
            )
        return session, input_value

    async def _read_json_body(self, request: web.Request) -> dict[str, object]:
        body = await self._limit_body(request)
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except ValueError, UnicodeDecodeError:
            raise CompanionWebError(
                400, {"status": "bad_request", "error": "BAD_REQUEST"}
            ) from None
        if not isinstance(value, dict):
            raise CompanionWebError(400, {"status": "bad_request", "error": "BAD_REQUEST"})
        return value


def _handoff_verified(result: object) -> bool:
    return bool(getattr(result, "verified", False))


def _handoff_claim(result: object) -> HandoffClaim:
    claim = getattr(result, "claim", None)
    if not isinstance(claim, HandoffClaim):
        raise CompanionWebError(500, {"status": "internal", "error": "INTERNAL"})
    return claim


__all__ = ["CompanionWebApp", "CompanionWebError"]
