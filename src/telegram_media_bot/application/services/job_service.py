from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.application.services.url_canonicalization import canonicalize_media_url
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    ImageDeliveryMode,
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

    def create_highlight_tray(
        self,
        *,
        chat_id: int,
        user_id: int,
        url: str,
        username: str,
    ) -> tuple[JobRecord, bool]:
        url = canonicalize_media_url(url).canonical_url
        key = _idempotency_key(
            kind=JobKind.HIGHLIGHT_TRAY,
            user_id=user_id,
            url=url,
            mode=None,
            container=None,
            selected_format_ids=(username,),
        )
        existing = self._repository.find_active_job(key)
        if existing is not None:
            return existing, False
        now = datetime.now(UTC)
        candidate = JobRecord(
            job_id=JobId(secrets.token_urlsafe(18)),
            kind=JobKind.HIGHLIGHT_TRAY,
            status=JobStatus.QUEUED,
            chat_id=chat_id,
            user_id=user_id,
            url=url,
            mode=None,
            idempotency_key=key,
            created_at=now,
            updated_at=now,
            selected_format_ids=(username,),
            url_classification="highlight_tray",
        )
        persisted = self._repository.create_job(candidate)
        return persisted, persisted.job_id == candidate.job_id

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
        selected_format_ids: tuple[str, ...] = (),
        image_delivery_mode: ImageDeliveryMode | None = None,
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
            selected_format_ids=selected_format_ids,
            image_delivery_mode=image_delivery_mode,
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
        selected_format_ids: tuple[str, ...] = (),
        image_delivery_mode: ImageDeliveryMode | None = None,
    ) -> tuple[JobRecord, bool]:
        intent = canonicalize_media_url(url)
        url = intent.canonical_url
        key = _idempotency_key(
            kind=kind,
            user_id=user_id,
            url=url,
            mode=mode,
            container=container,
            native_video_codec=native_video_codec,
            selected_format_ids=selected_format_ids,
            image_delivery_mode=image_delivery_mode,
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
            selected_format_ids=selected_format_ids,
            image_delivery_mode=image_delivery_mode,
            url_classification=intent.instagram_kind,
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
    selected_format_ids: tuple[str, ...] = (),
    image_delivery_mode: ImageDeliveryMode | None = None,
) -> str:
    parts = [kind.value, str(user_id), url, mode.value if mode else "inspect"]
    if container is not None:
        parts.append(container.value)
    if native_video_codec is not None:
        parts.append(native_video_codec.value)
    if image_delivery_mode is not None:
        parts.append(image_delivery_mode.value)
    parts.extend(selected_format_ids)
    material = "\x00".join(parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
