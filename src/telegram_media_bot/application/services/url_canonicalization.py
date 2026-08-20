from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)
_TWITTER_HOSTS = frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"})
_INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com"})
_TWITTER_STATUS_PATTERN = re.compile(
    r"^/(?P<username>[A-Za-z0-9_]{1,15})/status/(?P<status_id>[0-9]+)(?:/.*)?$"
)
# Instagram share/tracking parameters are stripped before routing; they carry no routing
# meaning and can contain user-generated share payloads.
_INSTAGRAM_PROFILE_PATTERN = re.compile(r"^/(?P<username>[A-Za-z0-9_.]+)/?$")
_INSTAGRAM_AVATAR_PATTERN = re.compile(r"^/(?P<username>[A-Za-z0-9_.]+)/avatar/?$")
_INSTAGRAM_POST_PATTERN = re.compile(r"^/(?:p|reel|reels|tv)/(?P<shortcode>[A-Za-z0-9_-]+)/?$")
_INSTAGRAM_STORY_PATTERN = re.compile(
    r"^/stories/(?P<username>[A-Za-z0-9_.]+)/(?P<media_id>[0-9]+)/?$"
)
_INSTAGRAM_STORY_ACCOUNT_PATTERN = re.compile(r"^/stories/(?P<username>[A-Za-z0-9_.]+)/?$")
_INSTAGRAM_HIGHLIGHT_PATTERN = re.compile(r"^/stories/highlights/(?P<highlight_id>[0-9]+)/?$")
_PLAYLIST_QUERY_PARAMETERS = frozenset(
    {
        "list",
        "start_radio",
        "index",
        "playnext",
        "pp",
        "si",
        "feature",
    }
)
_SAFE_LOG_QUERY_PARAMETERS = _PLAYLIST_QUERY_PARAMETERS | {"v", "t"}
_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PLAYLIST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{2,100}$")


@dataclass(frozen=True, slots=True)
class MediaUrlIntent:
    original_url: str
    canonical_url: str
    youtube_video_id: str | None = None
    youtube_playlist_id: str | None = None
    single_video_forced: bool = False
    youtube_playlist: bool = False
    removed_query_parameters: tuple[str, ...] = ()
    #: Explicit Instagram URL class per the routing contract: post, reel, story, story_account,
    #: profile, avatar, or highlight. A plain profile canonicalizes to its avatar target.
    instagram_kind: str | None = None

    @property
    def log_fields(self) -> dict[str, object]:
        return {
            "original_url": self.original_url,
            "canonical_url": self.canonical_url,
            "youtube_video_id": self.youtube_video_id,
            "youtube_playlist_id": self.youtube_playlist_id,
            "single_video_forced": self.single_video_forced,
            "removed_query_parameters": self.removed_query_parameters,
            "instagram_kind": self.instagram_kind,
        }


def canonicalize_media_url(url: str) -> MediaUrlIntent:
    candidate = url.strip()
    parsed = urlsplit(candidate)
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if parsed.scheme.casefold() in {"http", "https"} and hostname in _TWITTER_HOSTS:
        match = _TWITTER_STATUS_PATTERN.fullmatch(parsed.path)
        if match is not None:
            canonical = _rebuild_url(
                "https",
                "x.com",
                f"/{match.group('username')}/status/{match.group('status_id')}",
                (),
            )
            return MediaUrlIntent(original_url=canonical, canonical_url=canonical)
    if parsed.scheme.casefold() in {"http", "https"} and hostname in _INSTAGRAM_HOSTS:
        return _canonicalize_instagram(
            parsed.path,
            parse_qsl(parsed.query, keep_blank_values=True),
        )
    if parsed.scheme.casefold() not in {"http", "https"} or hostname not in _YOUTUBE_HOSTS:
        return MediaUrlIntent(original_url=candidate, canonical_url=candidate)

    query = parse_qsl(parsed.query, keep_blank_values=True)
    video_id = _youtube_video_id(hostname, parsed.path, query)
    playlist_id = _valid_playlist_id(_first_query_value(query, "list"))
    safe_original = _rebuild_url(
        parsed.scheme.casefold(),
        hostname,
        parsed.path,
        tuple((key, value) for key, value in query if key.casefold() in _SAFE_LOG_QUERY_PARAMETERS),
    )
    if video_id is None:
        return MediaUrlIntent(
            original_url=safe_original,
            canonical_url=urlunsplit(
                (parsed.scheme.casefold(), hostname, parsed.path, parsed.query, "")
            ),
            youtube_playlist_id=playlist_id,
            youtube_playlist=parsed.path.rstrip("/").casefold() == "/playlist"
            and playlist_id is not None,
        )

    removed = tuple(
        dict.fromkeys(
            key.casefold() for key, _value in query if key.casefold() in _PLAYLIST_QUERY_PARAMETERS
        )
    )
    canonical_query = (
        (("v", video_id),)
        if hostname != "youtu.be" and parsed.path.rstrip("/").casefold() == "/watch"
        else ()
    )
    return MediaUrlIntent(
        original_url=safe_original,
        canonical_url=_rebuild_url(
            parsed.scheme.casefold(),
            hostname,
            parsed.path,
            canonical_query,
        ),
        youtube_video_id=video_id,
        youtube_playlist_id=playlist_id,
        single_video_forced=True,
        removed_query_parameters=removed,
    )


def _canonicalize_instagram(path: str, query: list[tuple[str, str]]) -> MediaUrlIntent:
    """Canonicalize Instagram share/tracking URLs before routing (Part D contract).

    - Post/Reel/Story with an explicit media identity keep the identity and drop the query.
    - A Story with a media id downloads only that exact story item.
    - A plain profile URL is treated as a profile-avatar action and canonicalizes to the
      internal ``/USERNAME/avatar/`` gallery-dl target so a profile never silently downloads
      the account's post history.
    - A bare story-account URL (no media id) stays distinct and is rejected as bulk by the
      gallery adapter.
    """
    removed = tuple(dict.fromkeys(key.casefold() for key, _value in query))
    if _INSTAGRAM_HIGHLIGHT_PATTERN.fullmatch(path) is not None:
        canonical = f"https://www.instagram.com{path.rstrip('/')}/"
        return MediaUrlIntent(
            original_url=canonical,
            canonical_url=canonical,
            removed_query_parameters=removed,
            instagram_kind="highlight",
        )
    story = _INSTAGRAM_STORY_PATTERN.fullmatch(path)
    if story is not None:
        canonical = f"https://www.instagram.com/stories/{story.group('username')}/{story.group('media_id')}/"
        return MediaUrlIntent(
            original_url=canonical,
            canonical_url=canonical,
            removed_query_parameters=removed,
            instagram_kind="story",
        )
    if _INSTAGRAM_STORY_ACCOUNT_PATTERN.fullmatch(path) is not None:
        canonical = f"https://www.instagram.com{path.rstrip('/')}/"
        return MediaUrlIntent(
            original_url=canonical,
            canonical_url=canonical,
            removed_query_parameters=removed,
            instagram_kind="story_account",
        )
    post = _INSTAGRAM_POST_PATTERN.fullmatch(path)
    if post is not None:
        kind = (
            "reel"
            if path.strip("/").split("/", maxsplit=1)[0].casefold() in {"reel", "reels"}
            else "post"
        )
        canonical = f"https://www.instagram.com/{path.strip('/')}/"
        return MediaUrlIntent(
            original_url=canonical,
            canonical_url=canonical,
            removed_query_parameters=removed,
            instagram_kind=kind,
        )
    avatar = _INSTAGRAM_AVATAR_PATTERN.fullmatch(path)
    if avatar is not None:
        canonical = f"https://www.instagram.com/{avatar.group('username')}/avatar/"
        return MediaUrlIntent(
            original_url=canonical,
            canonical_url=canonical,
            removed_query_parameters=removed,
            instagram_kind="avatar",
        )
    profile = _INSTAGRAM_PROFILE_PATTERN.fullmatch(path)
    if profile is not None:
        canonical = f"https://www.instagram.com/{profile.group('username')}/avatar/"
        return MediaUrlIntent(
            original_url=canonical,
            canonical_url=canonical,
            removed_query_parameters=removed,
            instagram_kind="profile",
        )
    # Unknown Instagram path: keep the path, drop tracking query, and leave routing to adapters.
    canonical = f"https://www.instagram.com{path.rstrip('/')}/"
    return MediaUrlIntent(
        original_url=canonical,
        canonical_url=canonical,
        removed_query_parameters=removed,
        instagram_kind="unsupported",
    )


def _youtube_video_id(
    hostname: str,
    path: str,
    query: list[tuple[str, str]],
) -> str | None:
    if hostname == "youtu.be":
        path_video_id = path.strip("/").split("/", maxsplit=1)[0]
        return path_video_id if _VIDEO_ID_PATTERN.fullmatch(path_video_id) else None
    normalized_path = path.rstrip("/").casefold()
    if normalized_path == "/watch":
        query_video_id = _first_query_value(query, "v")
        return (
            query_video_id
            if query_video_id and _VIDEO_ID_PATTERN.fullmatch(query_video_id)
            else None
        )
    parts = path.strip("/").split("/")
    if len(parts) == 2 and parts[0].casefold() in {"shorts", "live"}:
        return parts[1] if _VIDEO_ID_PATTERN.fullmatch(parts[1]) else None
    return None


def _first_query_value(query: list[tuple[str, str]], name: str) -> str | None:
    return next((value for key, value in query if key.casefold() == name), None)


def _valid_playlist_id(value: str | None) -> str | None:
    return value if value and _PLAYLIST_ID_PATTERN.fullmatch(value) else None


def _rebuild_url(
    scheme: str,
    hostname: str,
    path: str,
    query: tuple[tuple[str, str], ...],
) -> str:
    return urlunsplit((scheme, hostname, path, urlencode(query, doseq=True), ""))
