"""Companion web application route/security tests (T016)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from telegram_media_bot.application.services.handoff import (
    CompanionHandoffService,
    HandoffLinkService,
)
from telegram_media_bot.bootstrap.companion import (
    DisabledInstagramConnectionFlow,
    EmptyProviderCallbackRegistry,
    UnavailablePaymentCallbackProcessor,
)
from telegram_media_bot.domain.web_companion import (
    HandoffPurpose,
    InstagramConnectStage,
    PaymentCallbackOutcome,
)
from telegram_media_bot.infrastructure.persistence.sqlite_handoff import (
    SqliteHandoffNonceRepository,
)
from telegram_media_bot.infrastructure.security.handoff import (
    Ed25519HandoffSigner,
    Ed25519HandoffVerifier,
)
from telegram_media_bot.infrastructure.web_companion.app import (
    _CSRF_HEADER,
    _SESSION_COOKIE,
    CompanionWebApp,
)


def _build(tmp_path: Path, **overrides: object):
    _signer, private = Ed25519HandoffSigner.generate()
    verifier = Ed25519HandoffVerifier.from_private_encoded(
        private, max_clock_skew_seconds=int(overrides.get("skew", 30))
    )
    link = HandoffLinkService(
        Ed25519HandoffSigner.from_encoded(private), lifetime=timedelta(minutes=5)
    )
    repo = SqliteHandoffNonceRepository(tmp_path / "handoff.sqlite3")
    repo.initialize()
    service = CompanionHandoffService(verifier=verifier, nonce_repository=repo)
    kwargs = {
        "host": "127.0.0.1",
        "port": 0,
        "session_max_seconds": 300,
        "interactive_flow_max_seconds": 600,
        "interactive_flow_max_sessions": 10,
        "body_limit_bytes": int(overrides.get("body_limit", 65536)),
        "read_timeout_seconds": 5.0,
        "rate_limit_per_minute": int(overrides.get("rate_limit", 60)),
        "trusted_proxies": (),
        "handoff_exchange": service.exchange,
        "flow": DisabledInstagramConnectionFlow(),
        "provider_registry": EmptyProviderCallbackRegistry(),
        "payment_processor": UnavailablePaymentCallbackProcessor(),
    }
    app = CompanionWebApp(**kwargs).build()
    return app, service, link


def _token(link: HandoffLinkService, owner: int = 7) -> str:
    return link.create(purpose=HandoffPurpose.INSTAGRAM_CONNECT, owner_user_id=owner)


async def test_exchange_success_sets_secure_cookie_and_csrf(tmp_path: Path) -> None:
    app, _svc, link = _build(tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/instagram/connect/exchange", json={"token": _token(link)})
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["csrf_token"]
        raw_set_cookie = resp.headers.get("Set-Cookie", "")
        assert "Secure" in raw_set_cookie
        assert "HttpOnly" in raw_set_cookie
        assert "SameSite=lax" in raw_set_cookie.replace("SameSite=Lax", "SameSite=lax")
        assert _SESSION_COOKIE in raw_set_cookie


async def test_exchange_rejects_replayed_token(tmp_path: Path) -> None:
    app, _svc, link = _build(tmp_path)
    token = _token(link)
    async with TestClient(TestServer(app)) as client:
        first = await client.post("/instagram/connect/exchange", json={"token": token})
        assert first.status == 200
        second = await client.post("/instagram/connect/exchange", json={"token": token})
        assert second.status == 401


async def test_flow_step_requires_cookie_and_csrf(tmp_path: Path) -> None:
    app, _svc, link = _build(tmp_path)
    async with TestClient(TestServer(app)) as client:
        exchange = await client.post("/instagram/connect/exchange", json={"token": _token(link)})
        session_id = _extract_cookie_value(exchange.headers.get("Set-Cookie", ""))
        csrf = (await exchange.json())["csrf_token"]
        # No CSRF header -> rejected.
        no_csrf = await client.post(
            "/instagram/connect/complete",
            json={"input": "x"},
            headers={"Cookie": f"{_SESSION_COOKIE}={session_id}"},
        )
        assert no_csrf.status == 403
        # With CSRF -> the disabled flow returns NOT_AVAILABLE (404), proving the route is wired.
        ok = await client.post(
            "/instagram/connect/complete",
            json={"input": "x"},
            headers={
                "Cookie": f"{_SESSION_COOKIE}={session_id}",
                _CSRF_HEADER: csrf,
            },
        )
        assert ok.status == 404
        assert (await ok.json())["stage"] == InstagramConnectStage.NOT_AVAILABLE.value


def _extract_cookie_value(set_cookie: str) -> str:
    for part in set_cookie.split(";"):
        key, _, value = part.partition("=")
        if key.strip() == _SESSION_COOKIE:
            return value.strip()
    raise AssertionError("session cookie not present in Set-Cookie")


async def test_security_headers_present(tmp_path: Path) -> None:
    app, _svc, _link = _build(tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        assert resp.status == 200
        assert "Content-Security-Policy" in resp.headers
        assert "Referrer-Policy" in resp.headers
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["X-Content-Type-Options"] == "nosniff"


async def test_payment_callback_no_registered_provider(tmp_path: Path) -> None:
    app, _svc, _link = _build(tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/payment/callback/example",
            data=b"some payload",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 404
        body = await resp.json()
        assert body["error"] == "NOT_FOUND"


async def test_body_size_limit_enforced(tmp_path: Path) -> None:
    app, _svc, _link = _build(tmp_path, body_limit=128)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/instagram/connect/exchange",
            json={"token": "x" * 2048},
        )
        assert resp.status == 413


async def test_rate_limit_enforced(tmp_path: Path) -> None:
    app, _svc, link = _build(tmp_path, rate_limit=1)
    token = _token(link)
    async with TestClient(TestServer(app)) as client:
        first = await client.post("/instagram/connect/exchange", json={"token": token})
        assert first.status == 200
        second = await client.post("/instagram/connect/exchange", json={"token": _token(link)})
        assert second.status == 429


async def test_bad_body_and_method_handling(tmp_path: Path) -> None:
    app, _svc, _link = _build(tmp_path)
    async with TestClient(TestServer(app)) as client:
        bad = await client.post(
            "/instagram/connect/exchange", data=b"not-json", headers={"Content-Type": "text/plain"}
        )
        assert bad.status == 400
        block = await client.post("/instagram/connect/exchange", json={"token": "not-a-real-token"})
        assert block.status == 401


async def test_verified_payment_callback_cannot_confirm_entitlement(tmp_path: Path) -> None:
    """A registered provider verifier still cannot confirm anything without the billing service."""

    class _Processor:
        async def process(self, *, provider_id: str, provider_payload: bytes):
            del provider_id, provider_payload
            return PaymentCallbackOutcome.NOT_AVAILABLE

    class _V:
        def verify_callback(self, provider_payload: bytes) -> bool:
            del provider_payload
            return True

    class _Registry:
        def verifier_for(self, provider_id: str):
            del provider_id
            return _V()

    _signer, private = Ed25519HandoffSigner.generate()
    verifier = Ed25519HandoffVerifier.from_private_encoded(private)
    service = CompanionHandoffService(
        verifier=verifier,
        nonce_repository=SqliteHandoffNonceRepository(tmp_path / "handoff.sqlite3"),
    )
    app = CompanionWebApp(
        host="127.0.0.1",
        port=0,
        session_max_seconds=300,
        interactive_flow_max_seconds=600,
        interactive_flow_max_sessions=10,
        body_limit_bytes=65536,
        read_timeout_seconds=5.0,
        rate_limit_per_minute=60,
        trusted_proxies=(),
        handoff_exchange=service.exchange,
        flow=DisabledInstagramConnectionFlow(),
        provider_registry=_Registry(),
        payment_processor=_Processor(),
    ).build()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/payment/callback/zarinpal", data=b"payload")
        assert resp.status == 404
        assert (await resp.json())["error"] == "NOT_FOUND"
