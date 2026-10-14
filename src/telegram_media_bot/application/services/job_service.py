from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.application.services.url_canonicalization import canonicalize_media_url
from telegram_media_bot.domain.credential_resolution import (
    ContentAccessScope,
    CredentialAttemptPhase,
    CredentialPolicy,
)
from telegram_media_bot.domain.errors import PrivateMediaAuthorizationError
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
    StoryDeliveryMode,
    normalize_container_policy,
)
from telegram_media_bot.domain.subscriptions import Capability, EntitlementSnapshot


class JobService:
    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository

    def create_inspection(
        self,
        *,
        chat_id: int,
        user_id: int,
        url: str,
        entitlement_snapshot: EntitlementSnapshot | None = None,
        credential_policy: CredentialPolicy | None = None,
        content_access_scope: ContentAccessScope = ContentAccessScope.UNKNOWN,
        user_credential_generation: int | None = None,
    ) -> tuple[JobRecord, bool]:
        return self._create(
            kind=JobKind.INSPECTION,
            chat_id=chat_id,
            user_id=user_id,
            url=url,
            mode=None,
            entitlement_snapshot=entitlement_snapshot,
            credential_policy=credential_policy,
            content_access_scope=content_access_scope,
            user_credential_generation=user_credential_generation,
        )

    def create_highlight_tray(
        self,
        *,
        chat_id: int,
        user_id: int,
        url: str,
        username: str,
        entitlement_snapshot: EntitlementSnapshot | None = None,
        credential_policy: CredentialPolicy | None = None,
        content_access_scope: ContentAccessScope = ContentAccessScope.UNKNOWN,
        user_credential_generation: int | None = None,
    ) -> tuple[JobRecord, bool]:
        return self._create(
            kind=JobKind.HIGHLIGHT_TRAY,
            chat_id=chat_id,
            user_id=user_id,
            url=url,
            mode=None,
            selected_format_ids=(username,),
            entitlement_snapshot=entitlement_snapshot,
            credential_policy=credential_policy,
            content_access_scope=content_access_scope,
            user_credential_generation=user_credential_generation,
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
        selected_format_ids: tuple[str, ...] = (),
        image_delivery_mode: ImageDeliveryMode | None = None,
        story_delivery_mode: StoryDeliveryMode | None = None,
        entitlement_snapshot: EntitlementSnapshot | None = None,
        credential_policy: CredentialPolicy | None = None,
        content_access_scope: ContentAccessScope = ContentAccessScope.UNKNOWN,
        user_credential_generation: int | None = None,
        operator_credential_generation: int | None = None,
        credential_attempt_phase: CredentialAttemptPhase = CredentialAttemptPhase.NOT_STARTED,
        fallback_used: bool = False,
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
            story_delivery_mode=story_delivery_mode,
            entitlement_snapshot=entitlement_snapshot,
            credential_policy=credential_policy,
            content_access_scope=content_access_scope,
            user_credential_generation=user_credential_generation,
            operator_credential_generation=operator_credential_generation,
            credential_attempt_phase=credential_attempt_phase,
            fallback_used=fallback_used,
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
        story_delivery_mode: StoryDeliveryMode | None = None,
        entitlement_snapshot: EntitlementSnapshot | None = None,
        credential_policy: CredentialPolicy | None = None,
        content_access_scope: ContentAccessScope = ContentAccessScope.UNKNOWN,
        user_credential_generation: int | None = None,
        operator_credential_generation: int | None = None,
        credential_attempt_phase: CredentialAttemptPhase = CredentialAttemptPhase.NOT_STARTED,
        fallback_used: bool = False,
    ) -> tuple[JobRecord, bool]:
        intent = canonicalize_media_url(url)
        url = intent.canonical_url
        if (
            credential_policy is CredentialPolicy.USER_ONLY
            or content_access_scope is ContentAccessScope.USER_RESTRICTED
            or (
                entitlement_snapshot is not None
                and entitlement_snapshot.capability is Capability.INSTAGRAM_PRIVATE_MEDIA
            )
        ):
            require_private_job_authorization(
                user_id=user_id,
                entitlement_snapshot=entitlement_snapshot,
                credential_policy=credential_policy,
                content_access_scope=content_access_scope,
                user_credential_generation=user_credential_generation,
                operator_credential_generation=operator_credential_generation,
                fallback_used=fallback_used,
                credential_attempt_phase=credential_attempt_phase,
            )
        key = _idempotency_key(
            kind=kind,
            user_id=user_id,
            url=url,
            mode=mode,
            container=container,
            native_video_codec=native_video_codec,
            selected_format_ids=selected_format_ids,
            image_delivery_mode=image_delivery_mode,
            story_delivery_mode=story_delivery_mode,
            credential_policy=credential_policy,
            user_credential_generation=user_credential_generation,
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
            story_delivery_mode=story_delivery_mode,
            url_classification=(
                "highlight_tray" if kind is JobKind.HIGHLIGHT_TRAY else intent.instagram_kind
            ),
            entitlement_snapshot=entitlement_snapshot,
            credential_policy=credential_policy,
            content_access_scope=content_access_scope,
            user_credential_generation=user_credential_generation,
            operator_credential_generation=operator_credential_generation,
            credential_attempt_phase=credential_attempt_phase,
            fallback_used=fallback_used,
        )
        persisted = self._repository.create_job(candidate)
        return persisted, persisted.job_id == candidate.job_id


def require_private_job_authorization(
    *,
    user_id: int,
    entitlement_snapshot: EntitlementSnapshot | None,
    credential_policy: CredentialPolicy | None,
    content_access_scope: ContentAccessScope,
    user_credential_generation: int | None,
    operator_credential_generation: int | None = None,
    fallback_used: bool = False,
    credential_attempt_phase: CredentialAttemptPhase = CredentialAttemptPhase.NOT_STARTED,
) -> None:
    """Validate the durable accepted capability, never the owner's current subscription."""
    snapshot = entitlement_snapshot
    if (
        snapshot is None
        or snapshot.capability is not Capability.INSTAGRAM_PRIVATE_MEDIA
        or snapshot.owner_user_id != user_id
        or snapshot.accepted_at.tzinfo is None
        or snapshot.authorized_until.tzinfo is None
        or snapshot.authorized_until <= snapshot.accepted_at
        or not snapshot.plan_id
        or not snapshot.grant_id
        or credential_policy is not CredentialPolicy.USER_ONLY
        or content_access_scope is not ContentAccessScope.USER_RESTRICTED
        or isinstance(user_credential_generation, bool)
        or not isinstance(user_credential_generation, int)
        or user_credential_generation < 1
        or operator_credential_generation is not None
        or fallback_used
        or credential_attempt_phase is CredentialAttemptPhase.OPERATOR_ATTEMPT
    ):
        raise PrivateMediaAuthorizationError("Private job has incomplete owner-bound authorization")


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
    story_delivery_mode: StoryDeliveryMode | None = None,
    credential_policy: CredentialPolicy | None = None,
    user_credential_generation: int | None = None,
) -> str:
    parts = [kind.value, str(user_id), url, mode.value if mode else "inspect"]
    if container is not None:
        parts.append(container.value)
    if native_video_codec is not None:
        parts.append(native_video_codec.value)
    if image_delivery_mode is not None:
        parts.append(image_delivery_mode.value)
    # Legacy Highlight downloads already mean normal delivery. Keep their active/
    # uncertain identity when a new explicit normal button is used after upgrade.
    if story_delivery_mode is not None and not (
        mode is DownloadMode.INSTAGRAM_HIGHLIGHT and story_delivery_mode is StoryDeliveryMode.NORMAL
    ):
        parts.append(story_delivery_mode.value)
    if credential_policy in {
        CredentialPolicy.USER_ONLY,
        CredentialPolicy.USER_FIRST_PUBLIC_FALLBACK,
    }:
        parts.append(f"credential:{credential_policy.value}")
        if user_credential_generation is not None:
            parts.append(f"user-generation:{user_credential_generation}")
    parts.extend(selected_format_ids)
    material = "\x00".join(parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
