from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.domain.credential_resolution import ContentAccessScope, CredentialPolicy
from telegram_media_bot.domain.errors import PrivateMediaAuthorizationError
from telegram_media_bot.domain.models import DownloadMode, JobKind
from telegram_media_bot.domain.subscriptions import Capability, EntitlementSnapshot, GrantId, PlanId
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository


def _accepted() -> EntitlementSnapshot:
    accepted = datetime.now(UTC) - timedelta(days=2)
    return EntitlementSnapshot(
        capability=Capability.INSTAGRAM_PRIVATE_MEDIA,
        accepted_at=accepted,
        authorized_until=accepted + timedelta(days=1),
        plan_id=PlanId("vip"),
        grant_id=GrantId("grant"),
        owner_user_id=20,
    )


def _create(service: JobService, kind: JobKind, arguments: dict[str, Any]) -> Any:
    if kind is JobKind.INSPECTION:
        return service.create_inspection(**arguments)
    if kind is JobKind.HIGHLIGHT_TRAY:
        return service.create_highlight_tray(username="owner", **arguments)
    return service.create_download(mode=DownloadMode.ALL_ORIGINAL_MEDIA, **arguments)


@pytest.mark.parametrize("kind", list(JobKind))
def test_private_job_accepts_only_complete_owner_bound_authorization(
    tmp_path: Path, kind: JobKind
) -> None:
    repository = SqliteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    service = JobService(repository)
    arguments: dict[str, Any] = {
        "chat_id": 10,
        "user_id": 20,
        "url": "https://www.instagram.com/p/accepted/",
        "entitlement_snapshot": _accepted(),
        "credential_policy": CredentialPolicy.USER_ONLY,
        "content_access_scope": ContentAccessScope.USER_RESTRICTED,
        "user_credential_generation": 1,
    }
    record, created = _create(service, kind, arguments)
    assert created
    persisted = repository.get_job(record.job_id)
    assert persisted is not None
    assert persisted.entitlement_snapshot is not None
    assert persisted.entitlement_snapshot == arguments["entitlement_snapshot"]
    assert persisted.entitlement_snapshot.owner_user_id == 20
    assert persisted.user_credential_generation == 1
    same, created = _create(service, kind, arguments)
    assert not created
    assert same.job_id == record.job_id
    arguments["user_credential_generation"] = 2
    changed, created = _create(service, kind, arguments)
    assert created and changed.job_id != record.job_id


@pytest.mark.parametrize("kind", list(JobKind))
@pytest.mark.parametrize(
    "changed",
    [
        {"entitlement_snapshot": None},
        {"entitlement_snapshot": replace(_accepted(), owner_user_id=99)},
        {"entitlement_snapshot": replace(_accepted(), owner_user_id=None)},
        {
            "entitlement_snapshot": replace(
                _accepted(), capability=Capability.INSTAGRAM_USER_SESSION_PREFERENCE
            )
        },
        {"credential_policy": None},
        {"credential_policy": CredentialPolicy.OPERATOR_PUBLIC},
        {"content_access_scope": ContentAccessScope.UNKNOWN},
        {"content_access_scope": ContentAccessScope.PUBLIC},
        {"user_credential_generation": None},
        {"user_credential_generation": 0},
        {"user_credential_generation": True},
    ],
)
def test_private_job_rejects_incomplete_or_cross_owner_acceptance(
    tmp_path: Path, kind: JobKind, changed: dict[str, Any]
) -> None:
    repository = SqliteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    arguments: dict[str, Any] = {
        "chat_id": 10,
        "user_id": 20,
        "url": "https://www.instagram.com/p/accepted/",
        "entitlement_snapshot": _accepted(),
        "credential_policy": CredentialPolicy.USER_ONLY,
        "content_access_scope": ContentAccessScope.USER_RESTRICTED,
        "user_credential_generation": 1,
    }
    arguments.update(changed)
    with pytest.raises(PrivateMediaAuthorizationError):
        _create(JobService(repository), kind, arguments)
    assert repository.counts().queued == 0


@pytest.mark.parametrize("kind", list(JobKind))
def test_free_public_job_requires_no_subscription_or_user_session(
    tmp_path: Path, kind: JobKind
) -> None:
    repository = SqliteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    record, created = _create(
        JobService(repository),
        kind,
        {"chat_id": 10, "user_id": 20, "url": "https://www.instagram.com/p/public/"},
    )
    assert created
    assert record.entitlement_snapshot is None
    assert record.user_credential_generation is None
    assert record.credential_policy is None
