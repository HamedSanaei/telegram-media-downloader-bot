from telegram_media_bot.domain.models import MediaKind
from telegram_media_bot.infrastructure.ytdlp.mapper import detect_kind, map_media_info, normalize_source


def test_normalizes_extractor_families_to_source() -> None:
    assert normalize_source({"extractor_key": "YoutubePlaylist"}) == "youtube"
    assert normalize_source({"extractor_key": "SoundcloudSet"}) == "soundcloud"
    assert normalize_source({"extractor_key": "InstagramStory"}) == "instagram"
    assert normalize_source({"extractor_key": "CustomExtractor"}) == "customextractor"


def test_detects_playlist_audio_image_and_unknown() -> None:
    assert detect_kind({"entries": []}) is MediaKind.PLAYLIST
    assert detect_kind({"vcodec": "none", "acodec": "opus"}) is MediaKind.AUDIO
    assert detect_kind({"ext": "webp"}) is MediaKind.IMAGE
    assert detect_kind({}) is MediaKind.UNKNOWN


def test_maps_upstream_dictionary_to_owned_model() -> None:
    info = map_media_info(
        {
            "id": "abc",
            "title": "Title",
            "extractor_key": "TikTok",
            "webpage_url": "https://example.test/video",
            "vcodec": "h264",
            "acodec": "aac",
            "duration": 12.8,
            "entries": None,
            "uploader": "Creator",
            "thumbnail": "https://example.test/image.jpg",
        },
        original_url="https://fallback.test",
    )
    assert info.media_id == "abc"
    assert info.source == "tiktok"
    assert info.kind is MediaKind.VIDEO
    assert info.duration_seconds == 12
    assert info.uploader == "Creator"
