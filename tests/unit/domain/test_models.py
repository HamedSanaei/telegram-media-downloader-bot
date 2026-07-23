from telegram_media_bot.domain.models import DownloadMode


def test_download_modes_are_stable_semantic_values() -> None:
    assert DownloadMode.VIDEO_720.value == "video_720"
    assert DownloadMode.AUDIO_MP3.value == "audio_mp3"
