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


def test_twitter_status_tracking_query_has_stable_identity() -> None:
    plain = canonicalize_media_url("https://x.com/example/status/1951000000000000000")
    shared = canonicalize_media_url(
        "https://twitter.com/example/status/1951000000000000000?s=20&t=tracking"
    )

    assert plain.canonical_url == shared.canonical_url
    assert plain.canonical_url == "https://x.com/example/status/1951000000000000000"


def test_instagram_story_tracking_url_keeps_exact_media_id_and_username() -> None:
    url = (
        "https://www.instagram.com/stories/arezoo.m.1997/3964254748584813861"
        "?utm_source=ig_story_item_share&igsh=MTdoejRnanY0cXNtMw=="
    )

    intent = canonicalize_media_url(url)

    assert intent.canonical_url == (
        "https://www.instagram.com/stories/arezoo.m.1997/3964254748584813861/"
    )
    assert intent.instagram_kind == "story"
    assert "utm_source" not in intent.canonical_url
    assert "igsh" not in intent.canonical_url


def test_instagram_post_reel_and_reels_share_urls_canonicalize() -> None:
    post = canonicalize_media_url(
        "https://www.instagram.com/p/AbC123/?igsh=share&utm_source=ig_share_sheet"
    )
    reel = canonicalize_media_url("https://www.instagram.com/reel/AbC123/?igsh=share")
    reels = canonicalize_media_url("https://www.instagram.com/reels/AbC123/?igsh=share")

    assert post.canonical_url == "https://www.instagram.com/p/AbC123/"
    assert post.instagram_kind == "post"
    assert reel.canonical_url == "https://www.instagram.com/reel/AbC123/"
    assert reel.instagram_kind == "reel"
    assert reels.canonical_url == "https://www.instagram.com/reels/AbC123/"
    assert reels.instagram_kind == "reel"


def test_plain_instagram_profile_canonicalizes_to_avatar_target() -> None:
    with_www = canonicalize_media_url("https://www.instagram.com/exampleuser/")
    bare = canonicalize_media_url("https://instagram.com/exampleuser")

    assert with_www.canonical_url == "https://www.instagram.com/exampleuser/avatar/"
    assert with_www.instagram_kind == "profile"
    assert bare.canonical_url == "https://www.instagram.com/exampleuser/avatar/"
    assert bare.instagram_kind == "profile"
    # The log-safe original URL never carries the avatar rewrite or any tracking payload.
    assert with_www.log_fields["original_url"] == "https://www.instagram.com/exampleuser/avatar/"


def test_explicit_avatar_url_is_classified_avatar() -> None:
    intent = canonicalize_media_url("https://www.instagram.com/exampleuser/avatar/")

    assert intent.canonical_url == "https://www.instagram.com/exampleuser/avatar/"
    assert intent.instagram_kind == "avatar"


def test_instagram_story_account_url_stays_distinct_and_unexpanded() -> None:
    intent = canonicalize_media_url(
        "https://www.instagram.com/stories/exampleuser/?utm_source=ig_story_share"
    )

    assert intent.canonical_url == "https://www.instagram.com/stories/exampleuser/"
    assert intent.instagram_kind == "story_account"


def test_instagram_highlight_url_canonicalizes_and_strips_tracking() -> None:
    url = (
        "https://www.instagram.com/stories/highlights/17841400308474925/"
        "?utm_source=ig_story_item_share&igsh=MTdoejRnanY0cXNtMw=="
    )

    intent = canonicalize_media_url(url)

    assert intent.canonical_url == (
        "https://www.instagram.com/stories/highlights/17841400308474925/"
    )
    assert intent.instagram_kind == "highlight"
    assert "utm_source" not in intent.canonical_url
    assert "igsh" not in intent.canonical_url


def test_instagram_user_highlights_tray_url_stays_distinct() -> None:
    intent = canonicalize_media_url("https://www.instagram.com/exampleuser/highlights/?igsh=share")

    assert intent.canonical_url == "https://www.instagram.com/exampleuser/highlights/"
    assert intent.instagram_kind == "unsupported"
