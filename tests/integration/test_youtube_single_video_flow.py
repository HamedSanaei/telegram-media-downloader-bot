from collections.abc import Callable
from pathlib import Path

from telegram_media_bot.application.services.download_service import DownloadService
from telegram_media_bot.application.services.url_canonicalization import canonicalize_media_url
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.models import (
    ComponentHealth,
    DownloadMode,
    DownloadRequest,
    DownloadResult,
    JobId,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
    OutputContainer,
    ProgressEvent,
)
from telegram_media_bot.infrastructure.ytdlp.options import YtDlpOptionsFactory

RAW_MIX_URL = "https://www.youtube.com/watch?v=DGbwtVtthu8&list=RDDGbwtVtthu8&start_radio=1"
CANONICAL_URL = "https://www.youtube.com/watch?v=DGbwtVtthu8"


class PlanningEngine:
    def __init__(self, settings: Settings) -> None:
        self.options = YtDlpOptionsFactory(settings)
        self.inspection_count = 0
        self.inspection_url: str | None = None
        self.inspection_noplaylist: bool | None = None
        self.download_url: str | None = None
        self.download_noplaylist: bool | None = None

    def inspect(self, url: str) -> MediaInfo:
        self.inspection_count += 1
        self.inspection_url = url
        intent = canonicalize_media_url(url)
        self.inspection_noplaylist = bool(
            self.options.inspect_options(single_video=intent.single_video_forced)["noplaylist"]
        )
        return MediaInfo(
            media_id="DGbwtVtthu8",
            title="Single video",
            source="youtube",
            kind=MediaKind.VIDEO,
            webpage_url=url,
            format_options=(
                MediaFormatOption(
                    mode=DownloadMode.VIDEO_1080,
                    container=OutputContainer.MP4,
                    selected_format_ids=("137", "140"),
                    video_codec="avc1.640028",
                    audio_codec="mp4a.40.2",
                ),
            ),
        )

    def download(
        self,
        request: DownloadRequest,
        *,
        progress: Callable[[ProgressEvent], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> DownloadResult:
        del progress, is_cancelled
        self.download_url = request.url
        self.download_noplaylist = bool(self.options.download_options(request)["noplaylist"])
        output = request.output_directory / "video.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video")
        return DownloadResult(
            job_id=request.job_id,
            media_id="DGbwtVtthu8",
            title="Single video",
            source="youtube",
            kind=MediaKind.VIDEO,
            file_path=output,
            file_size_bytes=5,
        )

    def health(self) -> ComponentHealth:
        return ComponentHealth(name="planning-engine", healthy=True)


def test_mix_url_runs_one_single_video_inspection_and_canonical_download_plan(
    settings: Settings,
    tmp_path: Path,
) -> None:
    engine = PlanningEngine(settings)
    service = DownloadService(engine, frozenset({"youtube"}), allow_playlists=False)

    info = service.inspect(RAW_MIX_URL)
    engine.download(
        DownloadRequest(
            job_id=JobId("youtube-single-video"),
            url=info.webpage_url,
            mode=DownloadMode.VIDEO_1080,
            output_directory=tmp_path / "youtube-single-video",
            container=OutputContainer.MP4,
        )
    )

    assert info.kind is MediaKind.VIDEO
    assert info.item_count is None
    assert info.format_options
    assert engine.inspection_count == 1
    assert engine.inspection_url == CANONICAL_URL
    assert engine.inspection_noplaylist is True
    assert engine.download_url == CANONICAL_URL
    assert engine.download_noplaylist is True
