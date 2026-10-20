# SPDX-License-Identifier: GPL-2.0-only
"""External gallery-dl extractors; imported only inside the vendor subprocess.

Instagram's current profile document requires the banner provider variable even
when the client does not request banner metadata. Keep vendor authentication,
privacy checks, media enumeration and error handling unchanged.
"""

from __future__ import annotations

from typing import Any

from gallery_dl import exception as _exceptions  # type: ignore[import-untyped]
from gallery_dl.extractor.instagram import (  # type: ignore[import-untyped]
    InstagramAPI as _API,
)
from gallery_dl.extractor.instagram import (
    InstagramAvatarExtractor as _Avatar,
)
from gallery_dl.extractor.instagram import (
    InstagramHighlightsExtractor as _Highlights,
)
from gallery_dl.extractor.instagram import (
    InstagramInfoExtractor as _Info,
)
from gallery_dl.extractor.instagram import (
    InstagramPostExtractor as _Post,
)
from gallery_dl.extractor.instagram import (
    InstagramStoriesExtractor as _Stories,
)

from telegram_media_bot.infrastructure.gallerydl.errors import (
    format_process_diagnostic,
    map_process_failure,
)

_PROFILE_BANNER_VARIABLE = (
    "__relay_internal__pv__PolarisProfileInformTreatmentBannerEnabledrelayprovider"
)


class _InstagramAPI(_API):  # type: ignore[misc]
    def _call_graphql(self, opname: str, variables: dict[str, Any], **kwargs: Any) -> Any:
        if opname == "PolarisProfilePageContentQuery":
            # This UI-only field is unused; false requests no additional banner data.
            variables.setdefault(_PROFILE_BANNER_VARIABLE, False)
        return super()._call_graphql(opname, variables, **kwargs)


class _ProfileQueryCompatibility:
    log: Any

    def _init(self) -> None:
        upstream: Any = super()
        upstream._init()
        self.api = _InstagramAPI(self)

    def request(self, url: str, **kwargs: Any) -> Any:
        upstream: Any = super()
        try:
            return upstream.request(url, **kwargs)
        except _exceptions.GalleryDLException as exc:
            # JSON-Lines DataJob otherwise discards the exception and exits zero.
            # Classify in memory; never emit its URL, response body, or raw message.
            evidence = f"{type(exc).__name__}: {str(exc)[:4096]}"
            response = getattr(exc, "response", None)
            if response is not None:
                evidence += f"\nHTTP status {response.status_code}"
                if cooldown := response.headers.get("Retry-After"):
                    evidence += f"\nRetry-After: {cooldown[:512]}"
            error = map_process_failure(0, evidence.encode("utf-8", errors="replace"))
            self.log.warning("%s", format_process_diagnostic(error))
            raise


class InstagramStoriesExtractor(_ProfileQueryCompatibility, _Stories):  # type: ignore[misc]
    """Preserve single Story, All Stories and direct Highlight behavior."""


class InstagramHighlightsExtractor(_ProfileQueryCompatibility, _Highlights):  # type: ignore[misc]
    """Preserve the authorized Highlight tray and its source ordering."""


class InstagramAvatarExtractor(_ProfileQueryCompatibility, _Avatar):  # type: ignore[misc]
    """Preserve avatar inspection using the same profile query contract."""


class InstagramInfoExtractor(_ProfileQueryCompatibility, _Info):  # type: ignore[misc]
    """Preserve profile probes without exporting vendor objects to the application."""


class InstagramPostExtractor(_ProfileQueryCompatibility, _Post):  # type: ignore[misc]
    """Retain Post/Reel HTTP failure evidence without changing media extraction."""
