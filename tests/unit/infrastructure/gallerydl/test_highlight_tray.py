from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    CollectionTooLargeError,
    GalleryDlAuthenticationRequiredError,
    GalleryDlOutputChangedError,
    MediaUnavailableError,
)
from telegram_media_bot.infrastructure.gallerydl.adapter import GalleryDlEngine
from telegram_media_bot.infrastructure.gallerydl.models import GalleryProcessResult
from telegram_media_bot.infrastructure.gallerydl.parser import (
    parse_highlight_tray,
    parse_inspection,
)
from telegram_media_bot.infrastructure.gallerydl.runner import GalleryDlRunner


def _fixture(name: str) -> bytes:
    return (Path("tests/fixtures/gallerydl") / name).read_bytes()


def _tray_payload() -> bytes:
    return _fixture("instagram-highlights.json")


def test_highlight_tray_parses_ids_titles_and_counts() -> None:
    items = parse_highlight_tray(_tray_payload(), expected_provider="instagram", max_highlights=100)
    assert len(items) == 2
    first, second = items
    assert first.highlight_id == "222"
    assert first.title == "سفر"
    assert first.item_count == 2
    assert second.highlight_id == "111"
    assert second.title == "زندگی"
    assert second.item_count == 1


def test_highlight_tray_rejects_non_numeric_ids() -> None:
    payload = (
        '[2,{"category":"instagram","subcategory":"highlights","post_id":"reel:abc"}]'
        "\n"
        '[3,"https://cdn.example.invalid/1.jpg",{"category":"instagram","subcategory":"highlights","post_id":"reel:abc","extension":"jpg","type":"image"}]'
        "\n"
    )
    with pytest.raises(GalleryDlOutputChangedError):
        parse_highlight_tray(payload.encode(), expected_provider="instagram", max_highlights=100)


def test_highlight_tray_empty_is_output_error() -> None:
    with pytest.raises(GalleryDlOutputChangedError):
        parse_highlight_tray(b"", expected_provider="instagram", max_highlights=100)


def test_highlight_tray_cap_enforced() -> None:
    with pytest.raises(CollectionTooLargeError):
        parse_highlight_tray(_tray_payload(), expected_provider="instagram", max_highlights=1)


def test_highlight_tray_provider_mismatch_rejected() -> None:
    with pytest.raises(GalleryDlOutputChangedError):
        parse_highlight_tray(_tray_payload(), expected_provider="tiktok", max_highlights=100)


def test_highlight_tray_rejects_cross_provider_events() -> None:
    events = [json.loads(line) for line in _tray_payload().splitlines()]
    events[-1][-1]["category"] = "twitter"
    payload = "\n".join(json.dumps(event) for event in events).encode()

    with pytest.raises(GalleryDlOutputChangedError):
        parse_highlight_tray(payload, expected_provider="instagram", max_highlights=100)


@pytest.mark.parametrize("post_id", ["9007199254740991000000000000000", "\uff11\uff12\uff13", ""])
def test_highlight_tray_rejects_invalid_routing_identity(post_id: str) -> None:
    payload = json.dumps(
        [2, {"category": "instagram", "subcategory": "highlights", "post_id": post_id}]
    ).encode()

    with pytest.raises(GalleryDlOutputChangedError):
        parse_highlight_tray(payload, expected_provider="instagram", max_highlights=100)


def test_highlight_media_ids_do_not_become_highlight_choices() -> None:
    items = parse_highlight_tray(_tray_payload(), expected_provider="instagram", max_highlights=2)

    assert {item.highlight_id for item in items} == {"222", "111"}
    assert sum(item.item_count for item in items) == 3


class _TrayRunner:
    def __init__(self, payload: bytes, stderr: bytes = b"") -> None:
        self.payload = payload
        self.stderr = stderr
        self.commands: list[list[str]] = []

    def run(self, args: list[str], **_kwargs: object) -> GalleryProcessResult:
        self.commands.append(args)
        return GalleryProcessResult(0, self.payload, self.stderr, 0.01)


@pytest.mark.parametrize(
    "stderr,error",
    [
        (b"", MediaUnavailableError),
        (b"HTTP 403 Forbidden: login required", GalleryDlAuthenticationRequiredError),
    ],
)
def test_highlight_browser_empty_result_preserves_authentication_evidence(
    settings: Settings, stderr: bytes, error: type[Exception]
) -> None:
    runner = _TrayRunner(b"", stderr)
    engine = GalleryDlEngine(settings, runner=cast(GalleryDlRunner, runner))

    with pytest.raises(error):
        engine.fetch_highlight_tray("exampleuser")

    assert len(runner.commands) == 1


def test_highlight_browser_rejects_profile_post_history() -> None:
    payload = _fixture("instagram-single.json")

    with pytest.raises(GalleryDlOutputChangedError):
        parse_highlight_tray(payload, expected_provider="instagram", max_highlights=100)


def test_one_highlight_inspection_keeps_every_media_item_in_source_order() -> None:
    payload = b"\n".join(_tray_payload().splitlines()[:3])
    inspection = parse_inspection(payload, expected_provider="instagram", max_assets=2)

    assert inspection.title == "سفر"
    assert [asset.index for asset in inspection.assets] == [1, 2]
    assert [asset.extension for asset in inspection.assets] == ["jpg", "mp4"]

    with pytest.raises(CollectionTooLargeError):
        parse_inspection(payload, expected_provider="instagram", max_assets=1)
