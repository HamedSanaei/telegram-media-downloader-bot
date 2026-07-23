from __future__ import annotations

from typing import Any

from yt_dlp.extractor.common import InfoExtractor


class ExamplePublicMediaIE(InfoExtractor):
    """Reference extractor for an operator-owned JSON media endpoint."""

    IE_NAME = "example_public_media"
    _VALID_URL = r"https?://media\.example\.org/items/(?P<id>[A-Za-z0-9_-]+)"

    def _real_extract(self, url: str) -> dict[str, Any]:
        media_id = self._match_id(url)
        manifest = self._download_json(f"https://media.example.org/api/items/{media_id}", media_id)
        return {
            "id": str(manifest.get("id") or media_id),
            "title": str(manifest["title"]),
            "url": str(manifest["media_url"]),
            "thumbnail": manifest.get("thumbnail_url"),
            "duration": manifest.get("duration_seconds"),
        }
