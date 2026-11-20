from __future__ import annotations

import multiprocessing
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import urlsplit

import pytest
from redis import Redis

from telegram_media_bot.application.services.download_service import DownloadService
from telegram_media_bot.application.services.instagram_admission import InstagramAdmissionService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.credential_resolution import (
    CredentialContext,
    CredentialKind,
    CredentialPolicy,
    ResolvedCredential,
)
from telegram_media_bot.domain.errors import GalleryDlRateLimitedError
from telegram_media_bot.domain.instagram_admission import InstagramAdmissionError
from telegram_media_bot.domain.models import DownloadMode, DownloadRequest, JobId
from telegram_media_bot.infrastructure.gallerydl.adapter import GalleryDlEngine
from telegram_media_bot.infrastructure.gallerydl.models import GalleryProcessResult
from telegram_media_bot.infrastructure.gallerydl.runner import GalleryDlRunner
from telegram_media_bot.infrastructure.media_engine_router import RoutedMediaEngine
from telegram_media_bot.infrastructure.persistence.sqlite_instagram_admission import (
    SqliteInstagramAdmissionRepository,
)
from telegram_media_bot.infrastructure.security.instagram_admission import (
    RedisInstagramPermitLock,
    instagram_identity,
)
from telegram_media_bot.infrastructure.ytdlp.engine import YtDlpEngine

pytestmark = pytest.mark.integration


class _ProcessLock:
    """Process-shared atomic lock double; actual Redis semantics have a separate opt-in test."""

    def __init__(self, tokens: Any, mutex: Any) -> None:
        self.tokens, self.mutex = tokens, mutex

    def acquire(self, identity: str, token: str) -> bool:
        with self.mutex:
            if identity in self.tokens:
                return False
            self.tokens[identity] = token
            return True

    def release(self, identity: str, token: str) -> None:
        with self.mutex:
            if self.tokens.get(identity) == token:
                del self.tokens[identity]


def _worker(
    path: str,
    tokens: Any,
    mutex: Any,
    entered: Any,
    release: Any,
    result: Any,
    hold: bool,
    redis_url: str | None,
) -> None:
    repository = SqliteInstagramAdmissionRepository(Path(path))
    lock = RedisInstagramPermitLock.create(redis_url) if redis_url else _ProcessLock(tokens, mutex)
    service = InstagramAdmissionService(repository, lock, spacing_seconds=0, max_wait_seconds=0.3)
    try:
        with service.operation("shared-fixture-operator", "operator"):
            result.put("provider")
            entered.set()
            if hold:
                assert release.wait(15)
                raise GalleryDlRateLimitedError("fixture", retry_after_seconds=10000)
    except InstagramAdmissionError as exc:
        result.put(exc.diagnostic_category)
    finally:
        if isinstance(lock, RedisInstagramPermitLock):
            lock.close()


@pytest.mark.parametrize("real_redis", [False, True])
def test_two_worker_processes_share_one_permit_and_restart_persistent_cooldown(
    tmp_path: Path,
    real_redis: bool,
) -> None:
    url = os.getenv("TEST_INSTAGRAM_REDIS_URL") if real_redis else None
    if real_redis and not url:
        pytest.skip("Set TEST_INSTAGRAM_REDIS_URL to an isolated disposable local Redis")
    if url:
        assert urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}
        client: Redis = Redis.from_url(url)
        client.delete("media-bot:instagram:permit:shared-fixture-operator")
        lock = RedisInstagramPermitLock(client)
        assert lock.acquire("shared-fixture-operator", "owner")
        lock.release("shared-fixture-operator", "stale-token")
        assert client.get("media-bot:instagram:permit:shared-fixture-operator") == b"owner"
        lock.release("shared-fixture-operator", "owner")
    path = tmp_path / "state.sqlite3"
    repository = SqliteInstagramAdmissionRepository(path)
    repository.initialize()
    ctx = multiprocessing.get_context("spawn")
    with ctx.Manager() as manager:
        tokens, mutex = manager.dict(), manager.Lock()
        entered, release = ctx.Event(), ctx.Event()
        result = ctx.Queue()
        processes = []
        try:
            first = ctx.Process(
                target=_worker, args=(str(path), tokens, mutex, entered, release, result, True, url)
            )
            processes.append(first)
            first.start()
            assert entered.wait(15)
            assert result.get(timeout=5) == "provider"
            second = ctx.Process(
                target=_worker,
                args=(str(path), tokens, mutex, entered, release, result, False, url),
            )
            processes.append(second)
            second.start()
            second.join(15)
            assert second.exitcode == 0
            assert result.get(timeout=5) == "active"
            release.set()
            first.join(15)
            assert first.exitcode == 0
            assert result.get(timeout=5) == "rate_limited"
            # A third, fresh worker cannot probe the same session, even if Redis lost all keys.
            if url:
                client.delete("media-bot:instagram:permit:shared-fixture-operator")
            third = ctx.Process(
                target=_worker,
                args=(str(path), tokens, mutex, entered, release, result, False, url),
            )
            processes.append(third)
            third.start()
            third.join(15)
            assert third.exitcode == 0
            assert result.get(timeout=5) == "rate_limited"
        finally:
            release.set()
            for process in processes:
                process.join(5)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            result.close()
    if url:
        client.close()


class _Lock:
    def acquire(self, identity: str, token: str) -> bool:
        return True

    def release(self, identity: str, token: str) -> None:
        pass


class _RateLimitedRunner(GalleryDlRunner):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def run(self, args: Sequence[str], **kwargs: Any) -> GalleryProcessResult:
        self.calls += 1
        return GalleryProcessResult(1, b"", b"HTTP 429\nRetry-After: 10000", 0.0)


def _admission(settings: Settings) -> InstagramAdmissionService:
    store = SqliteInstagramAdmissionRepository(settings.database_path())
    store.initialize()
    return InstagramAdmissionService(store, _Lock(), spacing_seconds=0)


def test_all_adapter_paths_and_queued_users_stop_after_operator_429(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = _admission(settings)
    runner = _RateLimitedRunner()
    gallery = GalleryDlEngine(settings, runner=runner, admission=admission)
    ytdlp = YtDlpEngine(settings, admission=admission)
    engine = RoutedMediaEngine(gallery, ytdlp)
    service = DownloadService(engine, frozenset({"instagram"}))
    with pytest.raises(InstagramAdmissionError, match="rate_limited"):
        service.inspect(
            "https://www.instagram.com/p/fixture/", credential=ResolvedCredential.operator_public()
        )
    with pytest.raises(InstagramAdmissionError):
        gallery.fetch_highlight_tray("fixture", credential=ResolvedCredential.operator_public())
    with pytest.raises(InstagramAdmissionError):
        service.download(
            job_id=JobId("second-user"),
            url="https://www.instagram.com/stories/highlights/123/",
            mode=DownloadMode.INSTAGRAM_HIGHLIGHT,
            output_directory=tmp_path / "download",
        )
    with pytest.raises(InstagramAdmissionError):
        ytdlp.inspect("https://www.instagram.com/reel/fixture/")
    request = DownloadRequest(
        job_id=JobId("queued-yt-dlp"),
        url="https://www.instagram.com/p/fixture/",
        mode=DownloadMode.BEST_ORIGINAL,
        output_directory=tmp_path / "yt-dlp",
    )
    with pytest.raises(InstagramAdmissionError):
        ytdlp.download(request)
    with pytest.raises(InstagramAdmissionError):
        ytdlp.download_instagram_video_children(
            request,
            expected_parent_media_id="fixture",
            expected_total_slots=1,
            expected_video_indices=(1,),
        )
    assert runner.calls == 1
    # An unrelated personal generation can reach the adapter. It receives its own independent circuit.
    credential = ResolvedCredential(
        CredentialContext(
            CredentialKind.USER_INSTAGRAM, CredentialPolicy.USER_ONLY, user_generation=2
        ),
        "leased.txt",
        owner_user_id=99,
    )
    with pytest.raises(InstagramAdmissionError):
        gallery.inspect("https://www.instagram.com/p/fixture/", credential=credential)
    assert runner.calls == 2
    assert len(admission.repository.snapshot(now=time(), burst_window=3600)) == 2


def test_inspection_then_download_charges_three_operations_without_reusing_ui_credentials(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PIL import Image

    class Runner(GalleryDlRunner):
        def __init__(self) -> None:
            super().__init__()
            self.inspections = self.transfers = 0

        def run(self, args: Sequence[str], **kwargs: Any) -> GalleryProcessResult:
            if "--dump-json" in args:
                self.inspections += 1
                return GalleryProcessResult(
                    0, Path("tests/fixtures/gallerydl/instagram-single.json").read_bytes(), b"", 0.0
                )
            self.transfers += 1
            path = Path(kwargs["output_directory"]) / "0001.jpg"
            Image.new("RGB", (16, 16)).save(path)
            return GalleryProcessResult(0, b"", b"", 0.0)

    admission = _admission(settings)
    runner = Runner()
    gallery = GalleryDlEngine(settings, runner=runner, admission=admission)
    monkeypatch.setattr(gallery._validator, "validate", lambda url: url)
    service = DownloadService(
        RoutedMediaEngine(gallery, YtDlpEngine(settings, admission=admission)),
        frozenset({"instagram"}),
    )
    url = "https://www.instagram.com/p/IG1/"
    service.inspect(url, credential=ResolvedCredential.operator_public())
    result = service.download(
        job_id=JobId("download"),
        url=url,
        mode=DownloadMode.IMAGE_ORIGINAL,
        output_directory=tmp_path / "media",
        credential=ResolvedCredential.operator_public(),
    )
    assert result.file_path.is_file()
    assert (runner.inspections, runner.transfers) == (2, 1)
    (state,) = admission.repository.snapshot(now=time(), burst_window=3600)
    assert state.daily_operations == 3 and not state.active


def test_operator_snapshot_pins_identity_and_secret_bytes_during_atomic_rotation(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cookie = tmp_path / "cookies.txt"
    before = "# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\told-fixture\n"
    after = before.replace("old-fixture", "rotated-fixture")
    cookie.write_text(before, encoding="utf-8")
    raw = settings.model_dump()
    raw["yt_dlp"]["cookies_file"] = cookie
    configured = Settings.model_validate(raw)
    admission = _admission(configured)
    identity, _ = instagram_identity(None, None, cookie)

    def rotate(*_args: str) -> None:
        cookie.write_text(after, encoding="utf-8")

    admission._observer = rotate
    observed: list[str] = []

    class Runner(_RateLimitedRunner):
        def run(self, args: Sequence[str], **kwargs: Any) -> GalleryProcessResult:
            observed.append(Path(args[args.index("--cookies") + 1]).read_text(encoding="utf-8"))
            return super().run(args, **kwargs)

    engine = GalleryDlEngine(configured, runner=Runner(), admission=admission)
    with pytest.raises(InstagramAdmissionError):
        engine.inspect("https://www.instagram.com/p/fixture/")
    assert observed == [before]
    assert cookie.read_text(encoding="utf-8") == after
    assert not list(configured.storage.temp_path().glob("instagram-admission-*"))
    states = admission.repository.snapshot(now=time(), burst_window=3600)
    assert states[0].identity == identity and states[0].reason == "rate_limited"


def test_additive_schema_is_idempotent_and_does_not_touch_existing_job_data(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT)")
        db.execute("INSERT INTO jobs VALUES ('fixture', 'delivery_uncertain')")
    repo = SqliteInstagramAdmissionRepository(path)
    repo.initialize()
    repo.initialize()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM jobs").fetchall() == [("fixture", "delivery_uncertain")]
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
