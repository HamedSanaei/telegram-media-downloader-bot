from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from functools import wraps
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from urllib.parse import urlsplit

from redis import Redis
from redis.exceptions import RedisError

from telegram_media_bot.application.services.instagram_admission import (
    InstagramAdmissionService,
    instagram_operation_workspace,
)
from telegram_media_bot.domain.credential_resolution import CredentialKind, ResolvedCredential
from telegram_media_bot.domain.errors import CookieManagementError
from telegram_media_bot.domain.instagram_admission import InstagramAdmissionError
from telegram_media_bot.domain.models import DownloadRequest
from telegram_media_bot.infrastructure.cookies.manager import NetscapeCookieManager

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisInstagramPermitLock:
    def __init__(self, redis: Redis, *, ttl_seconds: int = 86400) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    @classmethod
    def create(cls, url: str) -> RedisInstagramPermitLock:
        return cls(Redis.from_url(url, socket_timeout=3, socket_connect_timeout=3))

    def acquire(self, identity: str, token: str) -> bool:
        try:
            return bool(
                self._redis.set(
                    "media-bot:instagram:permit:" + identity, token, nx=True, ex=self._ttl
                )
            )
        except RedisError as exc:
            raise InstagramAdmissionError("coordination_unavailable") from exc

    def release(self, identity: str, token: str) -> None:
        try:
            self._redis.eval(_RELEASE, 1, "media-bot:instagram:permit:" + identity, token)
        except RedisError as exc:
            raise InstagramAdmissionError("coordination_unavailable") from exc

    def close(self) -> None:
        self._redis.close()

    def clear_reviewed(self, identity: str) -> None:
        """Offline operator recovery only, after all workers have been stopped."""
        try:
            self._redis.delete("media-bot:instagram:permit:" + identity)
        except RedisError as exc:
            raise InstagramAdmissionError("coordination_unavailable") from exc


def personal_instagram_identity(owner_user_id: int, generation: int) -> str:
    return hashlib.sha256(f"instagram:user:{owner_user_id}:{generation}".encode()).hexdigest()


def instagram_identity(
    credential: ResolvedCredential | None,
    cookie_file: str | None,
    canonical_cookie: Path | None,
) -> tuple[str, str]:
    if credential is not None and cookie_file is not None:
        raise ValueError("credential and cookie_file cannot both be supplied")
    if credential is not None and credential.context.kind is CredentialKind.USER_INSTAGRAM:
        if (
            credential.owner_user_id is None
            or credential.owner_user_id <= 0
            or credential.context.user_generation is None
            or credential.context.user_generation <= 0
        ):
            raise InstagramAdmissionError("credential_identity_unavailable")
        return personal_instagram_identity(
            credential.owner_user_id, credential.context.user_generation
        ), "personal"
    anonymous = credential is not None and credential.context.kind is CredentialKind.NONE
    path = Path(cookie_file) if cookie_file is not None else canonical_cookie
    if credential is not None and credential.materialized_cookie_path is not None:
        path = Path(credential.materialized_cookie_path)
    if anonymous or path is None:
        return hashlib.sha256(b"instagram:anonymous").hexdigest(), "anonymous"
    try:
        fingerprint = NetscapeCookieManager(path).instagram_session_fingerprint()
    except CookieManagementError as exc:
        raise InstagramAdmissionError("credential_identity_unavailable") from exc
    if fingerprint is None:
        return hashlib.sha256(b"instagram:anonymous").hexdigest(), "anonymous"
    digest = hashlib.sha256(("instagram:operator:" + fingerprint).encode()).hexdigest()
    return digest, "operator"


@contextmanager
def pin_operator_credential(
    credential: ResolvedCredential | None,
    cookie_file: str | None,
    canonical_cookie: Path | None,
    temp_root: Path,
) -> Iterator[ResolvedCredential | None]:
    if credential is not None and cookie_file is not None:
        raise ValueError("credential and cookie_file cannot both be supplied")
    if credential is not None and credential.context.kind in {
        CredentialKind.NONE,
        CredentialKind.USER_INSTAGRAM,
    }:
        yield credential
        return
    if credential is not None and credential.materialized_cookie_path is not None:
        yield credential
        return
    path = Path(cookie_file) if cookie_file is not None else canonical_cookie
    if path is None:
        yield credential
        return
    # Pin the bytes used by BOTH identity derivation and the vendor operation. An atomic
    # operator upload during spacing/waiting must not change which session owns the permit.
    temp_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="instagram-admission-", dir=temp_root) as directory:
        snapshot = Path(directory) / "cookies.txt"
        try:
            payload = NetscapeCookieManager(path).export_combined()
            with snapshot.open("xb") as target:
                os.chmod(snapshot, 0o600)
                target.write(payload)
        except (OSError, CookieManagementError) as exc:
            raise InstagramAdmissionError("credential_identity_unavailable") from exc
        yield replace(
            credential or ResolvedCredential.operator_public(),
            materialized_cookie_path=str(snapshot),
        )


def instagram_operation[**P, T](operation: Callable[P, T]) -> Callable[P, T]:
    """Guard adapter entry points, including router children and Highlight tray operations.

    Injected only by the worker composition root. No job/authorization policy lives here.
    Each decorated method is one extractor operation, not an assertion about HTTP request count.
    """

    @wraps(operation)
    def guarded(*args: P.args, **kwargs: P.kwargs) -> T:
        engine = cast(Any, args[0])
        admission: InstagramAdmissionService | None = engine._instagram_admission
        if admission is None:
            return operation(*args, **kwargs)
        target = (
            args[1]
            if len(args) > 1
            else kwargs.get("url", kwargs.get("request", kwargs.get("username")))
        )
        url = target.url if isinstance(target, DownloadRequest) else str(target)
        if operation.__name__ == "fetch_highlight_tray":
            url = f"https://www.instagram.com/{target}/highlights/"
        host = (urlsplit(url).hostname or "").casefold().rstrip(".")
        if host != "instagram.com" and not host.endswith(".instagram.com"):
            return operation(*args, **kwargs)
        with pin_operator_credential(
            cast(ResolvedCredential | None, kwargs.get("credential")),
            cast(str | None, kwargs.get("cookie_file")),
            engine._settings.effective_cookie_file(),
            instagram_operation_workspace(engine._settings.storage.temp_path()),
        ) as pinned:
            identity, kind = instagram_identity(
                pinned, None, engine._settings.effective_cookie_file()
            )
            with admission.operation(
                identity, kind, is_cancelled=cast(Any, kwargs.get("is_cancelled"))
            ):
                arguments: Any = dict(kwargs)
                arguments.pop("cookie_file", None)
                arguments["credential"] = pinned
                return operation(*args, **arguments)

    return guarded
