from pathlib import Path

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.models import DownloadMode, DownloadRequest, JobId
from telegram_media_bot.infrastructure.ytdlp.options import YtDlpOptionsFactory, final_media_files


def make_request(tmp_path: Path, mode: DownloadMode) -> DownloadRequest:
    return DownloadRequest(
        job_id=JobId("job"),
        url="https://example.test/video",
        mode=mode,
        output_directory=tmp_path,
    )


def test_inspect_options_do_not_download(settings: Settings) -> None:
    options = YtDlpOptionsFactory(settings).inspect_options()
    assert options["skip_download"] is True
    assert options["noplaylist"] is True


def test_semantic_mode_maps_to_configured_selector(settings: Settings, tmp_path: Path) -> None:
    factory = YtDlpOptionsFactory(settings)
    options = factory.download_options(make_request(tmp_path, DownloadMode.VIDEO_720))
    assert options["format"] == settings.media.formats.video_720
    assert "exec" not in options
    assert "external_downloader" not in options


def test_audio_mp3_adds_audio_postprocessor(settings: Settings, tmp_path: Path) -> None:
    options = YtDlpOptionsFactory(settings).download_options(
        make_request(tmp_path, DownloadMode.AUDIO_MP3)
    )
    assert options["postprocessors"][0]["key"] == "FFmpegExtractAudio"


def test_optional_proxy_cookie_and_user_agent_are_applied(settings: Settings, tmp_path: Path) -> None:
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    raw = settings.model_dump()
    raw["yt_dlp"]["cookies_file"] = cookie
    raw["yt_dlp"]["proxy"] = "socks5://localhost:1080"
    raw["yt_dlp"]["user_agent"] = "test-agent"
    configured = type(settings).model_validate(raw)
    options = YtDlpOptionsFactory(configured).inspect_options()
    assert options["cookiefile"] == str(cookie)
    assert options["proxy"] == "socks5://localhost:1080"
    assert options["user_agent"] == "test-agent"


def test_final_media_files_ignores_partial_files(tmp_path: Path) -> None:
    (tmp_path / "video.mp4").write_bytes(b"ok")
    (tmp_path / "video.mp4.part").write_bytes(b"partial")
    (tmp_path / ".tmp").mkdir()
    assert final_media_files(tmp_path) == [tmp_path / "video.mp4"]
