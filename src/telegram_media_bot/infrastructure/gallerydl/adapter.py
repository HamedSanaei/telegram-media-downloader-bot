from __future__ import annotations

import mimetypes
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from time import monotonic
from typing import Never
from urllib.parse import urlsplit

import structlog

from telegram_media_bot.application.ports.download_engine import (
    CancellationCheck,
    ProgressSink,
)
from telegram_media_bot.application.services.url_canonicalization import canonicalize_media_url
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.credential_resolution import ResolvedCredential
from telegram_media_bot.domain.errors import (
    CollectionTooLargeError,
    DownloadFailedError,
    GalleryDlExtractionError,
    GalleryDlOutputChangedError,
    GalleryDlUnavailableError,
    GalleryDlUnsupportedUrlError,
    ImageValidationError,
    JobCancelledError,
    MediaBotError,
    MediaUnavailableError,
    RateLimitedError,
)
from telegram_media_bot.domain.models import (
    COLLECTION_MODES,
    ComponentHealth,
    DownloadArtifact,
    DownloadMode,
    DownloadRequest,
    DownloadResult,
    HighlightItem,
    MediaAsset,
    MediaInfo,
    MediaKind,
)
from telegram_media_bot.infrastructure.archive.ordered_zip import OrderedZipBuilder
from telegram_media_bot.infrastructure.gallerydl.command_builder import (
    GalleryDlCommandBuilder,
    is_gallery_social_url,
)
from telegram_media_bot.infrastructure.gallerydl.errors import map_process_failure
from telegram_media_bot.infrastructure.gallerydl.mapper import map_gallery_info
from telegram_media_bot.infrastructure.gallerydl.models import (
    GalleryInspection,
    GalleryProcessResult,
)
from telegram_media_bot.infrastructure.gallerydl.parser import (
    parse_highlight_tray,
    parse_inspection,
    transient_asset_urls,
)
from telegram_media_bot.infrastructure.gallerydl.runner import GalleryDlRunner
from telegram_media_bot.infrastructure.image_validation import validate_image
from telegram_media_bot.infrastructure.security.url_safety import PublicUrlValidator
from telegram_media_bot.infrastructure.ytdlp.transcoder import is_inline_video_streamable

logger = structlog.get_logger(__name__)
GALLERY_DL_VERSION = "1.32.8"
_GALLERY_MODES = {
    DownloadMode.IMAGE_ORIGINAL,
    DownloadMode.IMAGES_ORIGINAL,
    DownloadMode.ALL_ORIGINAL_MEDIA,
    DownloadMode.IMAGES_ONLY,
    DownloadMode.VIDEOS_ONLY,
    DownloadMode.VIDEO_ORIGINAL,
    DownloadMode.IMAGES_ZIP,
    DownloadMode.INSTAGRAM_ALL_STORIES,
    DownloadMode.INSTAGRAM_HIGHLIGHT,
}


class GalleryDlEngine:
    def __init__(self, settings: Settings, runner: GalleryDlRunner | None = None) -> None:
        self._settings = settings
        self._runner = runner or GalleryDlRunner()
        self._commands = GalleryDlCommandBuilder(
            settings.gallery_dl,
            settings.effective_cookie_file(),
        )
        self._gate = threading.BoundedSemaphore(settings.gallery_dl.max_concurrent_processes)
        self._validator = PublicUrlValidator(
            reject_private_networks=settings.security.reject_private_network_urls
        )
        self._zip = OrderedZipBuilder()

    @staticmethod
    def owns_mode(mode: DownloadMode) -> bool:
        return mode in _GALLERY_MODES

    @staticmethod
    def is_gallery_social_url(url: str) -> bool:
        return is_gallery_social_url(url)

    def inspect(
        self,
        url: str,
        *,
        max_assets: int | None = None,
        credential: ResolvedCredential | None = None,
        cookie_file: str | None = None,
    ) -> MediaInfo:
        canonical = canonicalize_media_url(url).canonical_url
        provider, args = self._commands.inspection(
            canonical, credential=credential, cookie_file=cookie_file
        )
        if not self._settings.gallery_dl.enabled:
            raise GalleryDlUnavailableError("gallery-dl is disabled")
        started = monotonic()
        with self._process_slot():
            result = self._runner.run(
                args, timeout_seconds=self._settings.gallery_dl.timeout_seconds
            )
        if result.return_code != 0:
            self._raise_process_failure(result, provider=provider)
        if _empty_gallery_events(result.stdout):
            self._raise_empty_inspection(result, provider=provider, canonical=canonical)
        for transient_url in transient_asset_urls(result.stdout):
            self._validator.validate(transient_url)
        try:
            inspection = parse_inspection(
                result.stdout,
                expected_provider=provider,
                max_assets=max_assets or self._settings.gallery_dl.max_assets_per_job,
            )
        except GalleryDlOutputChangedError as exc:
            if _is_instagram_story_url(canonical) and _empty_gallery_events(result.stdout):
                raise MediaUnavailableError("Instagram story is expired or unavailable") from exc
            raise
        self._check_known_size(inspection, collection=url_is_collection(canonical))
        info = map_gallery_info(inspection, canonical)
        if len(info.assets) >= self._settings.gallery_dl.zip_threshold:
            info = replace(
                info,
                format_options=tuple(
                    sorted(
                        info.format_options,
                        key=lambda option: option.mode is not DownloadMode.IMAGES_ZIP,
                    )
                ),
            )
        logger.info(
            "gallery_dl_inspection_completed",
            adapter="gallery-dl",
            gallery_dl_version=GALLERY_DL_VERSION,
            source=provider,
            extractor=provider,
            asset_count=len(info.assets),
            media_kinds=sorted({asset.kind.value for asset in info.assets}),
            total_known_size=info.estimated_size_bytes,
            elapsed_seconds=round(monotonic() - started, 3),
        )
        return info

    def download(
        self,
        request: DownloadRequest,
        *,
        progress: ProgressSink | None = None,
        is_cancelled: CancellationCheck | None = None,
        credential: ResolvedCredential | None = None,
        cookie_file: str | None = None,
    ) -> DownloadResult:
        info = self.inspect(
            request.url,
            max_assets=request.max_assets,
            credential=credential,
            cookie_file=cookie_file,
        )
        return self.download_inspected(
            request,
            info,
            progress=progress,
            is_cancelled=is_cancelled,
            credential=credential,
            cookie_file=cookie_file,
        )

    def download_inspected(
        self,
        request: DownloadRequest,
        info: MediaInfo,
        *,
        progress: ProgressSink | None = None,
        is_cancelled: CancellationCheck | None = None,
        credential: ResolvedCredential | None = None,
        cookie_file: str | None = None,
    ) -> DownloadResult:
        del progress
        if request.mode not in _GALLERY_MODES:
            raise DownloadFailedError("The gallery adapter does not own this semantic mode")
        selected = set(request.selected_format_ids)
        planned = tuple(
            asset for asset in info.assets if not selected or asset.asset_id in selected
        )
        if request.mode in {
            DownloadMode.IMAGE_ORIGINAL,
            DownloadMode.IMAGES_ORIGINAL,
            DownloadMode.IMAGES_ONLY,
            DownloadMode.IMAGES_ZIP,
        }:
            planned = tuple(asset for asset in planned if asset.kind is MediaKind.IMAGE)
        elif request.mode in {
            DownloadMode.VIDEOS_ONLY,
            DownloadMode.VIDEO_ORIGINAL,
        }:
            planned = tuple(asset for asset in planned if asset.kind is MediaKind.VIDEO)
        if not planned:
            raise GalleryDlOutputChangedError("Selected gallery assets are no longer available")
        workspace = request.output_directory.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        if is_cancelled is not None and is_cancelled():
            raise JobCancelledError("Gallery download was cancelled")
        _cleanup_gallery_workspace(workspace)
        images_only = info.source.casefold() == "instagram" and request.mode in {
            DownloadMode.IMAGE_ORIGINAL,
            DownloadMode.IMAGES_ORIGINAL,
            DownloadMode.IMAGES_ONLY,
            DownloadMode.IMAGES_ZIP,
        }
        provider, args = self._commands.download(
            request.url,
            workspace,
            images_only=images_only,
            credential=credential,
            cookie_file=cookie_file,
        )
        try:
            with self._process_slot(is_cancelled):
                result = self._runner.run(
                    args,
                    timeout_seconds=self._settings.gallery_dl.timeout_seconds,
                    is_cancelled=is_cancelled,
                    output_directory=workspace,
                    max_output_bytes=(
                        self._max_collection_transfer_bytes
                        if request.mode in COLLECTION_MODES
                        else self._max_transfer_bytes
                    ),
                )
        except BaseException:
            _cleanup_gallery_workspace(workspace)
            raise
        if result.return_code != 0:
            _cleanup_gallery_workspace(workspace)
            self._raise_process_failure(result, provider=provider, job_id=str(request.job_id))
        try:
            files = _validated_output_files(workspace)
            downloaded_assets = (
                tuple(asset for asset in info.assets if asset.kind is MediaKind.IMAGE)
                if images_only
                else info.assets
            )
            if len(files) != len(downloaded_assets):
                raise GalleryDlOutputChangedError("gallery-dl file count differs from inspection")
            paired = tuple(zip(downloaded_assets, files, strict=True))
            kept: list[tuple[MediaAsset, Path]] = []
            selected_ids = {asset.asset_id for asset in planned}
            for asset, path in paired:
                if asset.asset_id not in selected_ids:
                    path.unlink(missing_ok=True)
                    continue
                if asset.kind is MediaKind.IMAGE:
                    validate_image(path, self._settings.gallery_dl.images)
                kept.append((asset, path))
            total = sum(path.stat().st_size for _, path in kept)
            transfer_limit = (
                self._max_collection_transfer_bytes
                if request.mode in COLLECTION_MODES
                else self._max_transfer_bytes
            )
            if total > transfer_limit:
                raise CollectionTooLargeError("Gallery output exceeds the configured total size")
        except BaseException:
            _cleanup_gallery_workspace(workspace)
            raise
        if request.mode is DownloadMode.IMAGES_ZIP:
            try:
                archive = self._zip.build(
                    [path for _, path in kept],
                    workspace / "original-images.zip",
                    is_cancelled=is_cancelled,
                )
            except BaseException:
                _cleanup_gallery_workspace(workspace)
                raise
            logger.info(
                "gallery_dl_download_completed",
                adapter="gallery-dl",
                gallery_dl_version=GALLERY_DL_VERSION,
                source=provider,
                job_id=request.job_id,
                asset_count=len(kept),
                media_kinds=[MediaKind.IMAGE.value],
                final_size_bytes=archive.stat().st_size,
                elapsed_seconds=round(result.elapsed_seconds, 3),
                delivery_plan="images_zip",
            )
            return DownloadResult(
                job_id=request.job_id,
                media_id=info.media_id,
                title=info.title,
                source=provider,
                kind=MediaKind.UNKNOWN,
                file_path=archive,
                file_size_bytes=archive.stat().st_size,
                mime_type="application/zip",
            )
        artifacts = tuple(
            DownloadArtifact(
                file_path=path,
                file_size_bytes=path.stat().st_size,
                kind=asset.kind,
                mime_type=mimetypes.guess_type(path.name)[0],
                title=asset.title or f"{info.title} {asset.index}",
                inline_video_streamable=(
                    is_inline_video_streamable(path) if asset.kind is MediaKind.VIDEO else False
                ),
                source_index=asset.index,
            )
            for asset, path in kept
        )
        first = artifacts[0]
        logger.info(
            "gallery_dl_download_completed",
            adapter="gallery-dl",
            gallery_dl_version=GALLERY_DL_VERSION,
            source=provider,
            job_id=request.job_id,
            asset_count=len(artifacts),
            media_kinds=sorted({artifact.kind.value for artifact in artifacts}),
            final_size_bytes=total,
            elapsed_seconds=round(result.elapsed_seconds, 3),
        )
        return DownloadResult(
            job_id=request.job_id,
            media_id=info.media_id,
            title=info.title,
            source=provider,
            kind=first.kind if len(artifacts) == 1 else MediaKind.PLAYLIST,
            file_path=first.file_path,
            file_size_bytes=total,
            mime_type=first.mime_type,
            artifacts=(
                artifacts if len(artifacts) > 1 or request.image_delivery_mode is not None else ()
            ),
            inline_video_streamable=first.inline_video_streamable,
            image_delivery_mode=request.image_delivery_mode,
        )

    def fetch_highlight_tray(
        self,
        username: str,
        *,
        max_highlights: int = 100,
    ) -> tuple[HighlightItem, ...]:
        """Fetch one Instagram account's highlight tray (authenticated, no media download)."""
        if not self._settings.gallery_dl.enabled:
            raise GalleryDlUnavailableError("gallery-dl is disabled")
        if "instagram" not in self._settings.gallery_dl.enabled_platforms:
            raise GalleryDlUnsupportedUrlError("Instagram is disabled")
        url = f"https://www.instagram.com/{username}/highlights/"
        args = self._commands.inspect_url("instagram", url)
        with self._process_slot():
            result = self._runner.run(
                args, timeout_seconds=self._settings.gallery_dl.timeout_seconds
            )
        if result.return_code != 0:
            self._raise_process_failure(result, provider="instagram")
        if _empty_gallery_events(result.stdout):
            raise GalleryDlOutputChangedError("gallery-dl emitted no highlight tray events")
        return parse_highlight_tray(
            result.stdout,
            expected_provider="instagram",
            max_highlights=max_highlights,
        )

    def health(self) -> ComponentHealth:
        if not self._settings.gallery_dl.enabled:
            return ComponentHealth("gallery_dl", True, "disabled")
        try:
            result = self._runner.run(self._commands.version(), timeout_seconds=10)
        except Exception as exc:
            return ComponentHealth("gallery_dl", False, type(exc).__name__)
        version = result.stdout.decode("utf-8", errors="replace").strip()
        return ComponentHealth(
            "gallery_dl",
            result.return_code == 0 and version == GALLERY_DL_VERSION,
            version,
        )

    def _check_known_size(self, inspection: GalleryInspection, *, collection: bool = False) -> None:
        known = [asset.size_bytes for asset in inspection.assets]
        if all(value is not None for value in known):
            total = sum(value or 0 for value in known)
            limit = self._max_collection_transfer_bytes if collection else self._max_transfer_bytes
            if total > limit:
                raise CollectionTooLargeError("Gallery metadata exceeds the total size limit")

    @property
    def _max_transfer_bytes(self) -> int:
        return (
            min(
                self._settings.gallery_dl.max_total_size_mb,
                self._settings.media.max_source_size_mb,
                self._settings.media.max_file_size_mb,
            )
            * 1024
            * 1024
        )

    @property
    def _max_collection_transfer_bytes(self) -> int:
        """Aggregate bound for batch collections; never the single-file upload limit."""
        return (
            min(
                self._settings.gallery_dl.max_total_size_mb,
                self._settings.media.max_source_size_mb,
            )
            * 1024
            * 1024
        )

    @staticmethod
    def _raise_process_failure(
        result: GalleryProcessResult,
        *,
        provider: str,
        job_id: str | None = None,
    ) -> None:
        error = map_process_failure(result.return_code, result.stderr)
        if isinstance(error, MediaBotError) and error.extractor is None:
            error.extractor = provider
        logger.warning(
            "gallery_dl_process_failed",
            adapter="gallery-dl",
            gallery_dl_version=GALLERY_DL_VERSION,
            source=provider,
            job_id=job_id,
            exit_classification=type(error).__name__,
            retryable=isinstance(error, (RateLimitedError, DownloadFailedError)),
            elapsed_seconds=round(result.elapsed_seconds, 3),
        )
        raise error

    @staticmethod
    def _raise_empty_inspection(
        result: GalleryProcessResult,
        *,
        provider: str,
        canonical: str,
    ) -> Never:
        error: Exception | None = None
        if result.stderr.strip():
            mapped = map_process_failure(result.return_code, result.stderr)
            if type(mapped) is not GalleryDlExtractionError:
                error = mapped
        if error is None:
            reason = (
                "Instagram story is expired or unavailable"
                if _is_instagram_story_url(canonical)
                else "gallery content is unavailable or inaccessible"
            )
            error = MediaUnavailableError(reason)
        if isinstance(error, MediaBotError) and error.extractor is None:
            error.extractor = provider
        logger.warning(
            "gallery_dl_zero_output",
            adapter="gallery-dl",
            extractor=provider,
            exit_code=result.return_code,
            stdout_event_count=0,
            stderr_category=type(error).__name__,
            stage="extraction",
        )
        raise error

    @contextmanager
    def _process_slot(self, is_cancelled: CancellationCheck | None = None) -> Iterator[None]:
        while not self._gate.acquire(timeout=0.1):
            if is_cancelled is not None and is_cancelled():
                raise JobCancelledError("Gallery download was cancelled while waiting")
        try:
            yield
        finally:
            self._gate.release()


def _is_instagram_story_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.hostname or "").casefold() in {"instagram.com", "www.instagram.com"} and (
        parsed.path.casefold().startswith("/stories/")
    )


def url_is_collection(url: str) -> bool:
    """Whether a canonical URL targets a multi-item gallery collection (stories/highlights tray)."""
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").casefold()
    if hostname not in {"instagram.com", "www.instagram.com"}:
        return False
    path = parsed.path.casefold()
    if path.startswith("/stories/highlights/"):
        return True
    # /stories/USERNAME/ with no media id -> all active stories collection.
    parts = path.strip("/").split("/")
    return len(parts) == 2 and parts[0] == "stories" and bool(parts[1])


def _empty_gallery_events(payload: bytes) -> bool:
    text = payload.decode("utf-8", errors="replace").strip()
    return not text or all(not line.strip() for line in text.splitlines())


def _validated_output_files(workspace: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for candidate in workspace.iterdir():
        resolved = candidate.resolve()
        if not resolved.is_relative_to(workspace) or candidate.is_symlink():
            raise ImageValidationError("Gallery output escapes the job workspace")
        if candidate.is_dir() or not candidate.is_file():
            raise ImageValidationError("Gallery output contains an unexpected entry")
        if candidate.name.endswith(".part") or candidate.stat().st_size <= 0:
            candidate.unlink(missing_ok=True)
            continue
        files.append(candidate)
    return tuple(sorted(files, key=lambda path: path.name))


def _cleanup_gallery_workspace(workspace: Path) -> None:
    if not workspace.is_dir():
        return
    for candidate in workspace.iterdir():
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink(missing_ok=True)
        elif candidate.is_dir():
            shutil.rmtree(candidate)
