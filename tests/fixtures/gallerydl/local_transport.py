"""Run the real gallery-dl CLI with Instagram/CDN HTTP routed to a loopback fixture.

Only the transport changes: extractor parsing, CLI configuration, downloads and cookie
finalization remain upstream behavior. Unexpected hosts fail closed without network access.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any
from urllib.parse import urlsplit

gallery_dl = importlib.import_module("gallery_dl")
requests = importlib.import_module("requests")

origin = sys.argv[1]
original_send = requests.Session.send


def local_send(self: Any, request: Any, **kwargs: Any) -> Any:
    parsed = urlsplit(request.url or "")
    if parsed.hostname not in {
        "www.instagram.com",
        "instagram.com",
        "static.cdninstagram.com",
        "cdn.fixture.invalid",
    }:
        raise RuntimeError("Offline fixture refused an unexpected host")
    request.url = origin + parsed.path + ("?" + parsed.query if parsed.query else "")
    kwargs["proxies"] = {}
    return original_send(self, request, **kwargs)


requests.Session.send = local_send
sys.argv = ["gallery-dl", "-o", "cache.file=:memory:", *sys.argv[2:]]
gallery_dl.main()
