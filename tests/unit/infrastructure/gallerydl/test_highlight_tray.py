from __future__ import annotations

from pathlib import Path

import pytest

from telegram_media_bot.domain.errors import (
    CollectionTooLargeError,
    GalleryDlOutputChangedError,
)
from telegram_media_bot.infrastructure.gallerydl.parser import parse_highlight_tray


def _fixture(name: str) -> bytes:
    return (Path("tests/fixtures/gallerydl") / name).read_bytes()


def _tray_payload() -> bytes:
    lines = [
        '[2,{"category":"instagram","id":"highlight:111","title":"سفر","subcategory":"highlights"}]',
        '[3,"https://cdn.example.invalid/h1/1.jpg",{"category":"instagram","id":"highlight:111","extension":"jpg","type":"image","media_id":"a1"}]',
        '[3,"https://cdn.example.invalid/h1/2.mp4",{"category":"instagram","id":"highlight:111","extension":"mp4","type":"video","media_id":"a2"}]',
        '[2,{"category":"instagram","id":"highlight:222","title":"زندگی","subcategory":"highlights"}]',
        '[3,"https://cdn.example.invalid/h2/1.jpg",{"category":"instagram","id":"highlight:222","extension":"jpg","type":"image","media_id":"b1"}]',
    ]
    return ("\n".join(lines) + "\n").encode()


def test_highlight_tray_parses_ids_titles_and_counts() -> None:
    items = parse_highlight_tray(_tray_payload(), expected_provider="instagram", max_highlights=100)
    assert len(items) == 2
    first, second = items
    assert first.highlight_id == "111"
    assert first.title == "سفر"
    assert first.item_count == 2
    assert second.highlight_id == "222"
    assert second.title == "زندگی"
    assert second.item_count == 1


def test_highlight_tray_rejects_non_numeric_ids() -> None:
    payload = (
        '[2,{"category":"instagram","id":"reel:abc","title":"x"}]'
        "\n"
        '[3,"https://cdn.example.invalid/1.jpg",{"category":"instagram","id":"reel:abc","extension":"jpg","type":"image"}]'
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
