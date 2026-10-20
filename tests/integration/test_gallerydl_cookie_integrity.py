from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import sys
import threading
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.credential_resolution import (
    CredentialContext,
    CredentialKind,
    CredentialPolicy,
    ResolvedCredential,
)
from telegram_media_bot.domain.errors import (
    GalleryDlAccessDeniedError,
    GalleryDlAuthenticationRequiredError,
    GalleryDlRateLimitedError,
    GalleryDlTransientError,
    MediaBotError,
)
from telegram_media_bot.domain.models import DownloadMode, DownloadRequest, JobId, MediaKind
from telegram_media_bot.infrastructure.gallerydl.adapter import GalleryDlEngine
from telegram_media_bot.infrastructure.gallerydl.command_builder import GalleryDlCommandBuilder
from telegram_media_bot.infrastructure.gallerydl.models import GalleryProcessResult
from telegram_media_bot.infrastructure.gallerydl.parser import parse_inspection
from telegram_media_bot.infrastructure.gallerydl.runner import GalleryDlRunner

pytestmark = pytest.mark.integration
_TRANSPORT = Path("tests/fixtures/gallerydl/local_transport.py").resolve()
_STORY_URL = "https://www.instagram.com/stories/fixtureuser/991/"
_VIDEO_BYTES = b"offline-story-video-content"


class _FixtureServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FixtureHandler)
        self.cookies_seen: list[str] = []
        self.requests_seen: list[str] = []
        self.profile_failure: int | str | None = None
        self.lock = threading.Lock()


class _FixtureHandler(BaseHTTPRequestHandler):
    server: _FixtureServer

    def do_GET(self) -> None:
        path = self._record_request()
        if path == "/fixtureuser/":
            payload = (
                b'<html><script id="__eqmc">{"l":"fixture-lsd","f":"fixture-dtsg"}</script>'
                b'<script>{"profile_id":"123","params":{"username":"fixtureuser"}}</script>'
                b'<link href="https://static.cdninstagram.com/rsrc.php/fixture.js"></html>'
            )
            self._respond(payload, "text/html")
        elif path == "/rsrc.php/fixture.js":
            operations = (
                "PolarisProfilePageContentQuery",
                "PolarisStoriesV3ReelPageGalleryQuery",
            )
            payload = "\n".join(
                f'__d("{name}_instagramRelayOperation",[],'
                f'(function(a,b,c,d,e,f){{e.exports="{index + 1000}"}}));'
                for index, name in enumerate(operations)
            ).encode()
            self._respond(payload, "application/javascript")
        elif path == "/story.mp4":
            self._respond(_VIDEO_BYTES, "video/mp4")
        elif path == "/accounts/login/":
            self._respond(b"<html>Fixture login required</html>", "text/html")
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = self._record_request()
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        form = parse_qs(body)
        operation = form.get("fb_api_req_friendly_name", [""])[0]
        variables = json.loads(form.get("variables", ["{}"])[0])
        user = {
            "id": "123",
            "pk": "123",
            "username": "fixtureuser",
            "full_name": "Fixture",
            "is_private": False,
        }
        if path == "/api/graphql" and operation == "PolarisProfilePageContentQuery":
            if self.server.profile_failure == "network":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                return
            if self.server.profile_failure == "login":
                self.send_response(302)
                self.send_header("Location", "https://www.instagram.com/accounts/login/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if isinstance(self.server.profile_failure, int):
                self.send_response(self.server.profile_failure)
                self.send_header("Content-Length", "0")
                if self.server.profile_failure in {429, 503}:
                    self.send_header("Retry-After", "120")
                self.end_headers()
                return
            # A non-null Boolean is now required by the actual provider document.
            banner = "__relay_internal__pv__PolarisProfileInformTreatmentBannerEnabledrelayprovider"
            response: dict[str, Any]
            if type(variables.get(banner)) is not bool:
                response = {
                    "errors": [
                        {
                            "message": "A server error missing_required_variable_value occured.",
                            "code": 1675012,
                            "is_transient": False,
                            "requires_reauth": False,
                            "allow_user_retry": False,
                        }
                    ],
                    "extensions": {"is_final": True},
                }
            else:
                response = {"data": {"user": user}}
        elif path == "/graphql/query" and operation == "PolarisStoriesV3ReelPageGalleryQuery":
            item = {
                "pk": "991",
                "code": "Story991",
                "taken_at": 1_700_000_000,
                "image_versions2": {
                    "candidates": [
                        {
                            "url": "https://cdn.fixture.invalid/cover.jpg",
                            "width": 1080,
                            "height": 1920,
                        }
                    ]
                },
                "video_versions": [
                    {
                        "url": "https://cdn.fixture.invalid/story.mp4",
                        "width": 1080,
                        "height": 1920,
                        "type": 101,
                    }
                ],
            }
            reel = {
                "id": "123",
                "user": user,
                "seen": 1_700_000_000,
                "expiring_at": 1_700_086_400,
                "items": [item],
            }
            response = {
                "data": {
                    "xdt_api__v1__feed__reels_media__connection": {
                        "edges": [{"node": reel}],
                        "page_info": {"has_next_page": False},
                    }
                }
            }
        else:
            self.send_error(404)
            return
        self._respond(json.dumps(response).encode(), "application/json")

    def _record_request(self) -> str:
        path = urlsplit(self.path).path
        with self.server.lock:
            self.server.cookies_seen.append(self.headers.get("Cookie", ""))
            self.server.requests_seen.append(path)
        return path

    def _respond(self, payload: bytes, content_type: str) -> None:
        self.send_response(200)
        # Exercise the real mutable session jar, while the source file must remain immutable.
        self.send_header("Set-Cookie", "fixture_rotated=changed; Path=/")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def local_gallery_server() -> Iterator[_FixtureServer]:
    server = _FixtureServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _local_args(args: Sequence[str], server: _FixtureServer) -> list[str]:
    origin = f"http://127.0.0.1:{server.server_port}"
    return [sys.executable, str(_TRANSPORT), origin, *args[3:]]


def _cookie_bytes(value: str) -> bytes:
    return (
        "# Netscape HTTP Cookie File\n"
        "# Fixture comment and provider records must survive byte-for-byte.\n"
        f".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t{value}\n"
        ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tunrelated-fixture-value\n"
    ).encode()


def _commands(settings: Settings, canonical: Path) -> GalleryDlCommandBuilder:
    return GalleryDlCommandBuilder(
        settings.gallery_dl.model_copy(update={"sleep_request_seconds": 0}), canonical
    )


async def test_real_gallery_subprocesses_keep_shared_and_user_cookie_jars_immutable(
    settings: Settings, tmp_path: Path, local_gallery_server: _FixtureServer
) -> None:
    canonical = tmp_path / "canonical.txt"
    user_jar = tmp_path / "owner-attempt" / "cookies.txt"
    user_jar.parent.mkdir()
    originals = {
        canonical: _cookie_bytes("operator-fixture"),
        user_jar: _cookie_bytes("user-fixture"),
    }
    for path, payload in originals.items():
        path.write_bytes(payload)
    before = {
        path: (hashlib.sha256(payload).digest(), path.stat().st_mtime_ns)
        for path, payload in originals.items()
    }
    credential = ResolvedCredential(
        CredentialContext(
            CredentialKind.USER_INSTAGRAM, CredentialPolicy.USER_ONLY, user_generation=7
        ),
        str(user_jar),
    )
    commands = _commands(settings, canonical)
    args_list: list[list[str]] = []
    workspaces: list[Path] = []
    for index, selected in enumerate((None, credential, None, credential)):
        workspace = tmp_path / f"download-{index}"
        workspace.mkdir()
        workspaces.append(workspace)
        args_list.append(commands.download(_STORY_URL, workspace, credential=selected)[1])
        args_list.append(commands.inspection(_STORY_URL, credential=selected)[1])
        # Same unrestricted builder used by source probes and tray browsing.
        args_list.append(commands.inspect_url("instagram", _STORY_URL, credential=selected))
    runner = GalleryDlRunner()
    results = await asyncio.gather(
        *(
            runner.run_async(_local_args(args, local_gallery_server), timeout_seconds=30)
            for args in args_list
        )
    )

    assert all(result.return_code == 0 for result in results)
    for index, result in enumerate(results):
        if index % 3:
            inspection = parse_inspection(
                result.stdout, expected_provider="instagram", max_assets=1
            )
            assert inspection.assets[0].kind is MediaKind.VIDEO
    for workspace in workspaces:
        assert [path.read_bytes() for path in workspace.iterdir()] == [_VIDEO_BYTES]
    for path, payload in originals.items():
        assert path.read_bytes() == payload
        assert (hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns) == before[path]
        assert not path.with_name(path.name + ".tmp").exists()
    assert any("sessionid=operator-fixture" in value for value in local_gallery_server.cookies_seen)
    assert any("sessionid=user-fixture" in value for value in local_gallery_server.cookies_seen)
    assert all(
        not ("operator-fixture" in value and "user-fixture" in value)
        for value in local_gallery_server.cookies_seen
    )


class _LocalRunner(GalleryDlRunner):
    def __init__(self, server: _FixtureServer) -> None:
        super().__init__()
        self.server = server
        self.inspections = 0

    def run(self, args: Sequence[str], **kwargs: Any) -> GalleryProcessResult:
        self.inspections += int("--dump-json" in args)
        return super().run(_local_args(args, self.server), **kwargs)


def test_real_instagram_story_video_extraction_and_download_preserve_adapter_contract(
    settings: Settings,
    tmp_path: Path,
    local_gallery_server: _FixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = settings.model_dump()
    raw["gallery_dl"]["sleep_request_seconds"] = 0
    configured = Settings.model_validate(raw)
    runner = _LocalRunner(local_gallery_server)
    engine = GalleryDlEngine(configured, runner=runner)
    monkeypatch.setattr(engine._validator, "validate", lambda url: url)
    monkeypatch.setattr(
        "telegram_media_bot.infrastructure.gallerydl.adapter.is_inline_video_streamable",
        lambda _path: False,
    )
    info = engine.inspect(_STORY_URL)
    assert info.source == "instagram"
    assert info.assets[0].source_post_id == "991"
    assert info.kind is MediaKind.VIDEO
    assert {option.mode for option in info.format_options} == {DownloadMode.VIDEO_ORIGINAL}
    result = engine.download_inspected(
        DownloadRequest(
            JobId("offline-story"), _STORY_URL, DownloadMode.VIDEO_ORIGINAL, tmp_path / "story"
        ),
        info,
    )
    assert result.kind is MediaKind.VIDEO
    assert result.file_path.read_bytes() == _VIDEO_BYTES
    assert result.file_size_bytes == len(_VIDEO_BYTES)
    assert runner.inspections == 1
    assert "cdn.fixture.invalid" not in repr(info)
    assert "ytdl:" not in repr(result)


@pytest.mark.parametrize(
    ("failure", "error_type", "retryable"),
    [
        (401, GalleryDlAuthenticationRequiredError, False),
        (403, GalleryDlAccessDeniedError, False),
        (429, GalleryDlRateLimitedError, True),
        (503, GalleryDlTransientError, True),
        ("network", GalleryDlTransientError, True),
        ("login", GalleryDlAuthenticationRequiredError, False),
    ],
)
def test_real_profile_failures_preserve_cause_without_extra_provider_requests(
    settings: Settings,
    local_gallery_server: _FixtureServer,
    monkeypatch: pytest.MonkeyPatch,
    failure: int | str,
    error_type: type[MediaBotError],
    retryable: bool,
) -> None:
    local_gallery_server.profile_failure = failure
    runner = _LocalRunner(local_gallery_server)
    engine = GalleryDlEngine(settings, runner=runner)
    monkeypatch.setattr(engine._validator, "validate", lambda url: url)

    with pytest.raises(error_type) as caught:
        engine.inspect(_STORY_URL)

    assert caught.value.retryable is retryable
    assert caught.value.http_status == (failure if isinstance(failure, int) else None)
    assert caught.value.retry_after_seconds == (120 if failure in {429, 503} else None)
    assert runner.inspections == 1
    assert local_gallery_server.requests_seen.count("/api/graphql") == 1
    assert "/graphql/query" not in local_gallery_server.requests_seen
    assert "/story.mp4" not in local_gallery_server.requests_seen
