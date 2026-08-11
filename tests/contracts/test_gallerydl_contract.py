from __future__ import annotations

import os
from pathlib import Path

import pytest

from telegram_media_bot.bootstrap.config import load_settings
from telegram_media_bot.domain.models import MediaKind
from telegram_media_bot.infrastructure.gallerydl.adapter import GalleryDlEngine

pytestmark = pytest.mark.contract


@pytest.mark.parametrize(
    ("source", "environment_name"),
    [
        ("instagram", "CONTRACT_GALLERYDL_INSTAGRAM_URL"),
        ("tiktok", "CONTRACT_GALLERYDL_TIKTOK_URL"),
        ("twitter", "CONTRACT_GALLERYDL_TWITTER_URL"),
        ("pinterest", "CONTRACT_GALLERYDL_PINTEREST_URL"),
    ],
)
def test_opt_in_gallerydl_normalized_contract(source: str, environment_name: str) -> None:
    if os.getenv("RUN_GALLERYDL_CONTRACT_TESTS") != "1":
        pytest.skip("set RUN_GALLERYDL_CONTRACT_TESTS=1 for live gallery-dl contracts")
    url = os.getenv(environment_name)
    config = os.getenv("GALLERYDL_CONTRACT_CONFIG")
    if not url or not config:
        pytest.skip(f"set {environment_name} and GALLERYDL_CONTRACT_CONFIG")

    info = GalleryDlEngine(load_settings(Path(config), require_token=False)).inspect(url)

    assert info.source == source
    assert info.assets
    assert any(asset.kind is MediaKind.IMAGE for asset in info.assets)
    assert all(asset.provider == source for asset in info.assets)
