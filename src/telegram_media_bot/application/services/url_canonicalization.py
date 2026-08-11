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
_TWITTER_STATUS_PATTERN = re.compile(
    r"^/(?P<username>[A-Za-z0-9_]{1,15})/status/(?P<status_id>[0-9]+)(?:/.*)?$"
)
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

    @property
    def log_fields(self) -> dict[str, object]:
        return {
            "original_url": self.original_url,
            "canonical_url": self.canonical_url,
            "youtube_video_id": self.youtube_video_id,
            "youtube_playlist_id": self.youtube_playlist_id,
            "single_video_forced": self.single_video_forced,
            "removed_query_parameters": self.removed_query_parameters,
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
