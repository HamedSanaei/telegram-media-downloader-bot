import json
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

import telegram_media_bot.bootstrap.companion as companion_module
from telegram_media_bot.bootstrap.companion import build_companion_app, load_companion_settings
from telegram_media_bot.bootstrap.config import load_settings
from telegram_media_bot.bootstrap.instagram import build_instagram_connection_service
from telegram_media_bot.bootstrap.instagram_setup import configure_instagram_connection
from telegram_media_bot.domain.instagram_credentials import InstagramCredentialState
from telegram_media_bot.infrastructure.instagram_login.real import RealInstagramSessionAcquirer
from telegram_media_bot.infrastructure.payments.base import ProviderHttpResponse


class LoginTransport:
    """Only provider transport is simulated; production service, crypto, routes and vault run."""

    def __init__(self, identity: int) -> None:
        self.identity = identity
        self.code_submissions = 0
        self.wrong_password = True

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
        if method == "GET":
            return ProviderHttpResponse(
                200,
                {
                    "set-cookie": f"csrftoken=csrf-{self.identity}; Path=/\nmid=mid-{self.identity}; Path=/"
                },
                b"page",
            )
        assert form is not None
        if url.endswith("/accounts/login/ajax/"):
            assert form["username"] == "same-instagram-user"
            if "wrong" in form["enc_password"]:
                return ProviderHttpResponse(
                    200, {}, b'{"authenticated":false,"message":"password incorrect"}'
                )
            return ProviderHttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "two_factor_required": True,
                        "two_factor_info": {"two_factor_identifier": f"challenge-{self.identity}"},
                    }
                ).encode(),
            )
        assert url.endswith("/accounts/ajax/two_factor_ajax/")
        assert form["username"] == "same-instagram-user"
        assert form["two_factor_identifier"] == f"challenge-{self.identity}"
        assert headers is not None and f"csrftoken=csrf-{self.identity}" in headers["Cookie"]
        self.code_submissions += 1
        return ProviderHttpResponse(
            200,
            {
                "set-cookie": f"sessionid=session-{self.identity}; Path=/\nds_user_id=42; Path=/\nrur=FRC; Path=/"
            },
            b'{"authenticated":true,"status":"ok"}',
        )


@pytest.fixture
def connection_config(tmp_path: Path) -> Path:
    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    raw["storage"]["root_directory"] = str(tmp_path / "data")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    configure_instagram_connection(path, public_base_url="https://connect.example.test")
    return path


async def test_real_two_factor_flow_is_owner_isolated_and_stores_no_passwords(
    connection_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = connection_config
    services: list[LoginTransport] = []
    real = RealInstagramSessionAcquirer

    def factory(*, timeout_seconds: float = 20) -> RealInstagramSessionAcquirer:
        transport = LoginTransport(len(services) + 1)
        services.append(transport)
        return real(requester=transport, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(companion_module, "RealInstagramSessionAcquirer", factory)
    connection = build_instagram_connection_service(load_settings(path))
    assert connection is not None
    app = build_companion_app(load_companion_settings(path.with_name("companion.local.yaml")))
    async with TestClient(TestServer(app)) as client:
        sessions = []
        for owner in (7, 8):
            token = urlsplit(connection.create_connect_link(owner)).fragment.removeprefix(
                "handoff="
            )
            exchange = await client.post("/instagram/connect/exchange", json={"token": token})
            assert exchange.status == 200
            session = exchange.cookies["tmb_web_session"].value
            headers = {
                "Cookie": f"tmb_web_session={session}",
                "X-TMB-CSRF": (await exchange.json())["csrf_token"],
            }
            # A first empty request, followed by wrong and then corrected credentials, is reusable.
            first = await client.post(
                "/instagram/connect/complete", headers=headers, json={"input": None}
            )
            assert (await first.json())["stage"] == "need_credentials"
            missing_consent = await client.post(
                "/instagram/connect/complete",
                headers=headers,
                json={
                    "input": {
                        "username": "same-instagram-user",
                        "password": "fixture-wrong",  # pragma: allowlist secret - synthetic rejected login
                    }
                },
            )
            assert missing_consent.status == 400
            denied = await client.post(
                "/instagram/connect/complete",
                headers=headers,
                json={
                    "input": {
                        "username": "same-instagram-user",
                        "password": "fixture-wrong",  # pragma: allowlist secret - synthetic rejected login
                        "consent": "yes",
                    }
                },
            )
            assert denied.status == 403
            challenge = await client.post(
                "/instagram/connect/complete",
                headers=headers,
                json={
                    "input": {
                        "username": "same-instagram-user",
                        "password": "fixture-valid",  # pragma: allowlist secret - synthetic provider fixture
                        "consent": "yes",
                    }
                },
            )
            assert (await challenge.json())["stage"] == "need_2fa"
            sessions.append((owner, headers))
        for owner, headers in sessions:
            result = await client.post(
                "/instagram/connect/complete", headers=headers, json={"input": {"code": "123456"}}
            )
            assert result.status == 200 and (await result.json())["stage"] == "connected"
            assert result.headers["Cache-Control"] == "no-store"
            view = connection.status(owner)
            assert view is not None and view.state is InstagramCredentialState.CONNECTED
            repeat = await client.post(
                "/instagram/connect/complete", headers=headers, json={"input": {"code": "123456"}}
            )
            assert repeat.status == 401
        assert sum(transport.code_submissions for transport in services) == 2
    database = load_settings(path).database_path()
    for name in (database, Path(str(database) + "-wal")):
        if name.exists():
            content = name.read_bytes()
            for secret in (
                b"fixture-wrong",
                b"fixture-valid",
                b"session-",
                b"same-instagram-user",
                b"123456",
            ):
                assert secret not in content
