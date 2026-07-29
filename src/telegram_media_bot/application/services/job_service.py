from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    JobId,
    JobKind,
    JobRecord,
    JobStatus,
    NativeVideoCodec,
    OutputContainer,
    normalize_container_policy,
)


class JobService:
    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository

    def create_inspection(self, *, chat_id: int, user_id: int, url: str) -> tuple[JobRecord, bool]:
        return self._create(
            kind=JobKind.INSPECTION,
            chat_id=chat_id,
            user_id=user_id,
            url=url,
            mode=None,
        )

    def create_download(
        self,
        *,
        chat_id: int,
        user_id: int,
        url: str,
        mode: DownloadMode,
        container: OutputContainer | None = None,
        container_policy: ContainerPolicy = ContainerPolicy.NATIVE_ONLY,
        native_video_codec: NativeVideoCodec | None = None,
    ) -> tuple[JobRecord, bool]:
        container_policy = normalize_container_policy(mode, container_policy)
        return self._create(
            kind=JobKind.DOWNLOAD,
            chat_id=chat_id,
            user_id=user_id,
            url=url,
            mode=mode,
            container=container,
            container_policy=container_policy,
            native_video_codec=native_video_codec,
        )

    def _create(
        self,
        *,
        kind: JobKind,
        chat_id: int,
        user_id: int,
        url: str,
        mode: DownloadMode | None,
        container: OutputContainer | None = None,
        container_policy: ContainerPolicy = ContainerPolicy.NATIVE_ONLY,
        native_video_codec: NativeVideoCodec | None = None,
    ) -> tuple[JobRecord, bool]:
        key = _idempotency_key(
            kind=kind,
            user_id=user_id,
            url=url,
            mode=mode,
            container=container,
            native_video_codec=native_video_codec,
        )
        existing = self._repository.find_active_job(key)
        if existing is not None:
            return existing, False
        now = datetime.now(UTC)
        candidate = JobRecord(
            job_id=JobId(secrets.token_urlsafe(18)),
            kind=kind,
            status=JobStatus.QUEUED,
            chat_id=chat_id,
            user_id=user_id,
            url=url,
            mode=mode,
            idempotency_key=key,
            created_at=now,
            updated_at=now,
            container=container,
            container_policy=container_policy,
            native_video_codec=native_video_codec,
        )
        persisted = self._repository.create_job(candidate)
        return persisted, persisted.job_id == candidate.job_id


def _idempotency_key(
    *,
    kind: JobKind,
    user_id: int,
    url: str,
    mode: DownloadMode | None,
    container: OutputContainer | None = None,
    native_video_codec: NativeVideoCodec | None = None,
) -> str:
    parts = [kind.value, str(user_id), url, mode.value if mode else "inspect"]
    if container is not None:
        parts.append(container.value)
    if native_video_codec is not None:
        parts.append(native_video_codec.value)
    material = "\x00".join(parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
