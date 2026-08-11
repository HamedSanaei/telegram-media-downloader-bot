"""Offline deterministic runtime smoke for the pinned gallery-dl adapter contract."""

from __future__ import annotations

import json

from telegram_media_bot.domain.models import MediaKind
from telegram_media_bot.infrastructure.gallerydl.parser import parse_inspection


def main() -> int:
    payload = json.dumps(
        [
            [
                3,
                "https://cdn.example.invalid/one.jpg",
                {
                    "category": "twitter",
                    "tweet_id": "fixture-1",
                    "extension": "jpg",
                    "type": "image",
                    "width": 1200,
                    "height": 900,
                },
            ],
            [
                3,
                "https://cdn.example.invalid/two.mp4",
                {
                    "category": "twitter",
                    "tweet_id": "fixture-1",
                    "extension": "mp4",
                    "type": "video",
                    "duration": 4,
                },
            ],
        ],
        separators=(",", ":"),
    ).encode()
    inspection = parse_inspection(payload, expected_provider="twitter", max_assets=2)
    assert tuple(asset.kind for asset in inspection.assets) == (
        MediaKind.IMAGE,
        MediaKind.VIDEO,
    )
    assert inspection.assets[0].asset_id != inspection.assets[1].asset_id
    print("gallery-dl deterministic adapter smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
