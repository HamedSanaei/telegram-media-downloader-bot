from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MAX_COOKIE_UPLOAD_BYTES = 2 * 1024 * 1024


class CookieService(StrEnum):
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    TWITTER = "twitter"
    PINTEREST = "pinterest"
    SOUNDCLOUD = "soundcloud"


#: Single source of truth for the supported cookie providers: service -> domains and display
#: label. Both the canonical cookie store and the Cookie Health Center consume this registry,
#: so the provider list is never hard-coded in multiple layers.
COOKIE_PROVIDER_REGISTRY: tuple[tuple[CookieService, tuple[str, ...], str], ...] = (
    (
        CookieService.YOUTUBE,
        ("youtube.com", "youtu.be", "google.com", "googlevideo.com"),
        "YouTube",
    ),
    (CookieService.INSTAGRAM, ("instagram.com",), "Instagram"),
    (CookieService.TIKTOK, ("tiktok.com",), "TikTok"),
    (CookieService.TWITTER, ("twitter.com", "x.com"), "X/Twitter"),
    (CookieService.PINTEREST, ("pinterest.com", "pin.it"), "Pinterest"),
    (CookieService.SOUNDCLOUD, ("soundcloud.com",), "SoundCloud"),
)

COOKIE_SERVICE_LABELS: dict[CookieService, str] = {
    service: label for service, _domains, label in COOKIE_PROVIDER_REGISTRY
}


def cookie_service_for_domain(domain: str) -> CookieService | None:
    """Return the supported cookie service for a hostname, or ``None`` when unsupported."""
    hostname = domain.lstrip(".").casefold()
    for service, suffixes, _label in COOKIE_PROVIDER_REGISTRY:
        if any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in suffixes):
            return service
    return None


def cookie_provider_domains(service: CookieService) -> tuple[str, ...]:
    return next(
        (domains for item, domains, _label in COOKIE_PROVIDER_REGISTRY if item is service), ()
    )


@dataclass(frozen=True, slots=True)
class CookieUpdateSummary:
    services: tuple[CookieService, ...]
    replaced: int
    added: int
    uploaded_record_count: int = 0
    previous_canonical_record_count: int = 0
    new_canonical_record_count: int = 0
    preserved_other_provider_count: int = 0
    provider_record_counts: tuple[tuple[CookieService, int], ...] = ()

    def record_count(self, provider: CookieService) -> int:
        return next(
            (count for service, count in self.provider_record_counts if service is provider),
            0,
        )
