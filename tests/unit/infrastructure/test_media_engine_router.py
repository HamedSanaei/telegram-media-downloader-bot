from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from telegram_media_bot.application.ports.download_engine import InstagramVideoDownloadEngine
from telegram_media_bot.application.services.download_service import DownloadService
from telegram_media_bot.domain.credential_resolution import (
    CredentialContext,
    CredentialKind,
    CredentialPolicy,
    ResolvedCredential,
)
from telegram_media_bot.domain.errors import CollectionTooLargeError
from telegram_media_bot.domain.models import (
    DownloadMode,
    DownloadRequest,
    DownloadResult,
    JobId,
    MediaAsset,
    MediaInfo,
    MediaInspection,
    MediaKind,
)
from telegram_media_bot.infrastructure.gallerydl.adapter import GalleryDlEngine
from telegram_media_bot.infrastructure.media_engine_router import RoutedMediaEngine


class RecordingGallery:
    def __init__(self) -> None:
        self.inspections: list[tuple[str, dict[str, Any]]] = []
        self.transfers: list[tuple[DownloadRequest, MediaInfo, dict[str, Any]]] = []
        self.asset_count = 1

    @staticmethod
    def owns_mode(_mode: DownloadMode) -> bool:
        return True

    def inspect(self, url: str, **kwargs: Any) -> MediaInfo:
        self.inspections.append((url, kwargs))
        return MediaInfo(
            "post",
            "Current post",
            "instagram",
            MediaKind.IMAGE,
            url,
            assets=tuple(
                MediaAsset(
                    index,
                    f"asset-{index}",
                    MediaKind.IMAGE,
                    "jpg",
                    "image/jpeg",
                    "post",
                    "instagram",
                )
                for index in range(1, self.asset_count + 1)
            ),
        )

    def download_inspected(
        self, request: DownloadRequest, info: MediaInfo, **kwargs: Any
    ) -> DownloadResult:
        self.transfers.append((request, info, kwargs))
        return DownloadResult(
            request.job_id,
            info.media_id,
            info.title,
            info.source,
            info.kind,
            request.output_directory / "photo.jpg",
            4,
        )


class UnusedVideoEngine:
    def download(self, _request: DownloadRequest, **_kwargs: Any) -> DownloadResult:
        raise AssertionError("Image-only plan must not reach the video adapter")


def _router(gallery: RecordingGallery) -> RoutedMediaEngine:
    return RoutedMediaEngine(
        cast(GalleryDlEngine, gallery), cast(InstagramVideoDownloadEngine, UnusedVideoEngine())
    )


def _credential(generation: int = 1, path: str = "attempt/cookies.txt") -> ResolvedCredential:
    return ResolvedCredential(
        CredentialContext(CredentialKind.USER_INSTAGRAM, CredentialPolicy.USER_ONLY, generation),
        path,
    )


def test_service_router_reuses_one_validated_extraction_in_same_credential_operation(
    tmp_path: Path,
) -> None:
    gallery = RecordingGallery()
    credential = _credential()
    service = DownloadService(_router(gallery), frozenset({"instagram"}))

    result = service.download(
        job_id=JobId("one-inspection"),
        url="https://www.instagram.com/p/post/",
        mode=DownloadMode.IMAGE_ORIGINAL,
        output_directory=tmp_path,
        credential=credential,
    )

    assert result.media_id == "post"
    assert len(gallery.inspections) == 1
    assert len(gallery.transfers) == 1
    request, info, arguments = gallery.transfers[0]
    assert request.inspection is not None
    assert info is request.inspection.media
    assert arguments["credential"] is credential
    assert gallery.inspections[0][1]["credential"] is credential


@pytest.mark.parametrize(
    "mismatch", ["url", "credential_kind", "generation", "lease", "cookie_file", "cap"]
)
def test_router_rejects_inspection_from_another_operation_and_inspects_once(
    tmp_path: Path, mismatch: str
) -> None:
    gallery = RecordingGallery()
    credential = _credential()
    url = "https://www.instagram.com/p/post/"
    old = MediaInfo("old", "Old post", "instagram", MediaKind.IMAGE, url)
    bound_credential = credential
    cookie_file = None
    bound_cookie_file = None
    max_assets = None
    if mismatch == "url":
        old = replace(old, webpage_url="https://www.instagram.com/p/other/")
    elif mismatch == "credential_kind":
        bound_credential = ResolvedCredential.operator_public()
    elif mismatch == "generation":
        bound_credential = _credential(2)
    elif mismatch == "lease":
        bound_credential = _credential(path="previous-attempt/cookies.txt")
    elif mismatch == "cookie_file":
        bound_cookie_file = "previous-attempt/cookies.txt"
    else:
        max_assets = 1
        old = replace(
            old,
            assets=tuple(
                MediaAsset(
                    index, str(index), MediaKind.IMAGE, "jpg", "image/jpeg", "old", "instagram"
                )
                for index in (1, 2)
            ),
        )
    request = DownloadRequest(
        JobId("mismatch"),
        url,
        DownloadMode.IMAGE_ORIGINAL,
        tmp_path,
        max_assets=max_assets,
        inspection=MediaInspection(old, bound_credential, bound_cookie_file),
    )

    result = _router(gallery).download(request, credential=credential, cookie_file=cookie_file)

    assert result.media_id == "post"
    assert len(gallery.inspections) == 1
    assert gallery.transfers[0][1].media_id == "post"


@pytest.mark.parametrize(
    "mode", [DownloadMode.INSTAGRAM_ALL_STORIES, DownloadMode.INSTAGRAM_HIGHLIGHT]
)
def test_collection_cap_applies_to_first_extraction_and_prevents_reuse_bypass(
    tmp_path: Path, mode: DownloadMode
) -> None:
    gallery = RecordingGallery()
    gallery.asset_count = 3
    service = DownloadService(
        _router(gallery),
        frozenset({"instagram"}),
        instagram_max_stories=2,
        instagram_max_highlight_items=2,
    )

    with pytest.raises(CollectionTooLargeError):
        service.download(
            job_id=JobId("bounded"),
            url="https://www.instagram.com/stories/account/",
            mode=mode,
            output_directory=tmp_path,
        )

    assert len(gallery.inspections) == 1
    assert gallery.inspections[0][1]["max_assets"] == 2
    assert gallery.transfers == []
