import pytest

from telegram_media_bot.application.services.url_canonicalization import canonicalize_media_url

VIDEO_ID = "DGbwtVtthu8"


def test_youtube_mix_watch_url_is_forced_to_single_video() -> None:
    intent = canonicalize_media_url(
        f"https://www.youtube.com/watch?v={VIDEO_ID}&list=RD{VIDEO_ID}&start_radio=1"
    )

    assert intent.canonical_url == f"https://www.youtube.com/watch?v={VIDEO_ID}"
    assert intent.youtube_video_id == VIDEO_ID
    assert intent.youtube_playlist_id == f"RD{VIDEO_ID}"
    assert intent.single_video_forced
    assert intent.removed_query_parameters == ("list", "start_radio")
    assert not intent.youtube_playlist


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list=PL123&index=5",
            f"https://www.youtube.com/watch?v={VIDEO_ID}",
        ),
        (
            f"https://youtu.be/{VIDEO_ID}?list=PL123",
            f"https://youtu.be/{VIDEO_ID}",
        ),
        (
            f"https://www.youtube.com/shorts/{VIDEO_ID}?list=PL123",
            f"https://www.youtube.com/shorts/{VIDEO_ID}",
        ),
        (
            f"https://www.youtube.com/live/{VIDEO_ID}?list=PL123",
            f"https://www.youtube.com/live/{VIDEO_ID}",
        ),
        (
            f"https://m.youtube.com/watch?v={VIDEO_ID}&feature=shared",
            f"https://m.youtube.com/watch?v={VIDEO_ID}",
        ),
        (
            f"https://music.youtube.com/watch?v={VIDEO_ID}&si=tracking",
            f"https://music.youtube.com/watch?v={VIDEO_ID}",
        ),
        (
            f"https://youtube.com/watch?v={VIDEO_ID}&playnext=1&pp=opaque",
            f"https://youtube.com/watch?v={VIDEO_ID}",
        ),
    ],
)
def test_supported_youtube_video_shapes_remove_playlist_context(
    url: str,
    expected: str,
) -> None:
    intent = canonicalize_media_url(url)

    assert intent.canonical_url == expected
    assert intent.youtube_video_id == VIDEO_ID
    assert intent.single_video_forced


def test_plain_watch_url_is_unchanged_and_forced_single() -> None:
    url = f"https://www.youtube.com/watch?v={VIDEO_ID}"

    intent = canonicalize_media_url(url)

    assert intent.canonical_url == url
    assert intent.single_video_forced
    assert intent.removed_query_parameters == ()


def test_playlist_url_remains_a_real_playlist() -> None:
    url = "https://www.youtube.com/playlist?list=PL123"

    intent = canonicalize_media_url(url)

    assert intent.canonical_url == url
    assert intent.youtube_video_id is None
    assert intent.youtube_playlist_id == "PL123"
    assert not intent.single_video_forced
    assert intent.youtube_playlist


def test_invalid_video_id_does_not_force_single_video() -> None:
    intent = canonicalize_media_url("https://www.youtube.com/watch?v=too-short&list=PL123")

    assert intent.youtube_video_id is None
    assert not intent.single_video_forced


def test_log_original_drops_unknown_credential_like_query_names() -> None:
    intent = canonicalize_media_url(
        f"https://www.youtube.com/watch?v={VIDEO_ID}&list=PL123&token=secret-value"
    )

    assert "token" not in intent.original_url
    assert "secret-value" not in intent.original_url
    assert "token" not in intent.canonical_url
    assert "secret-value" not in intent.canonical_url
    assert intent.log_fields["original_url"] == (
        f"https://www.youtube.com/watch?v={VIDEO_ID}&list=PL123"
    )
