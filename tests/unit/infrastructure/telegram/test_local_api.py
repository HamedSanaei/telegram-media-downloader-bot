from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import LocalBotApiError
from telegram_media_bot.infrastructure.telegram import local_api as local_api_module
from telegram_media_bot.infrastructure.telegram.local_api import (
    LocalBotApiManager,
    ManagedLocalApiHandle,
    MigrationStore,
)


def _local_settings(
    settings: Settings,
    tmp_path: Path,
    *,
    mode: str = "external",
) -> Settings:
    executable = tmp_path / "telegram-bot-api"
    executable.write_bytes(b"executable")
    raw = settings.model_dump()
    raw["telegram"]["local_api_base_url"] = "http://127.0.0.1:18081"
    raw["telegram"]["local_api_is_local"] = True
    raw["telegram"]["max_upload_size_mb"] = 1900
    raw["telegram"]["local_bot_api"] = {
        "enabled": True,
        "mode": mode,
        "executable": str(executable),
        "api_id": 12345,
        "api_hash": "LOCAL_API_HASH_FOR_TESTS",
        "host": "127.0.0.1",
        "port": 18081,
        "local_mode": True,
        "working_directory": str(tmp_path / "server"),
        "temp_directory": str(tmp_path / "server" / "temp"),
        "log_file": str(tmp_path / "server" / "server.log"),
        "verbosity": 2,
        "auto_start": True,
        "startup_timeout_seconds": 2,
        "shutdown_timeout_seconds": 1,
        "migration": {
            "auto_logout_from_cloud": False,
            "state_file": str(tmp_path / "state" / "migration.json"),
        },
    }
    raw["media"]["max_file_size_mb"] = 1900
    raw["media"]["max_source_size_mb"] = 2000
    return Settings.model_validate(raw)


async def test_migrate_to_local_logs_out_from_cloud_only_once(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = _local_settings(settings, tmp_path)
    manager = LocalBotApiManager(configured)
    calls: list[str] = []

    async def call_bot_method(**kwargs: Any) -> None:
        calls.append(str(kwargs["method"]))

    async def local_reachable() -> bool:
        return True

    monkeypatch.setattr(local_api_module, "_call_bot_method", call_bot_method)
    monkeypatch.setattr(manager, "ensure_started", lambda: None)
    monkeypatch.setattr(manager, "_local_bot_reachable", local_reachable)

    first = await manager.migrate_to_local()
    second = await manager.migrate_to_local()

    assert first.phase == "local"
    assert second.phase == "local"
    assert calls == ["logout"]


async def test_uncertain_cloud_logout_is_never_repeated(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = _local_settings(settings, tmp_path)
    manager = LocalBotApiManager(configured)
    calls = 0

    async def failed_call(**_kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise LocalBotApiError("safe failure")

    monkeypatch.setattr(local_api_module, "_call_bot_method", failed_call)
    monkeypatch.setattr(manager, "ensure_started", lambda: None)

    with pytest.raises(LocalBotApiError):
        await manager.migrate_to_local()
    with pytest.raises(LocalBotApiError):
        await manager.migrate_to_local()

    assert calls == 1
    assert manager.migration_store.read().phase == "cloud_logout_uncertain"


async def test_migrate_to_cloud_logs_out_locally_and_enters_wait(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = _local_settings(settings, tmp_path)
    manager = LocalBotApiManager(configured)
    manager.migration_store.write("local")
    calls: list[tuple[str, str | None]] = []

    async def call_bot_method(**kwargs: Any) -> None:
        calls.append((str(kwargs["method"]), kwargs["base_url"]))

    monkeypatch.setattr(local_api_module, "_call_bot_method", call_bot_method)
    monkeypatch.setattr(manager, "endpoint_reachable", lambda: True)

    state = await manager.migrate_to_cloud()
    repeated = await manager.migrate_to_cloud()

    assert state.phase == "cloud_wait"
    assert repeated.phase == "cloud_wait"
    assert state.cloud_available_after is not None
    assert calls == [("logout", "http://127.0.0.1:18081")]


def test_managed_command_line_never_contains_api_credentials(
    settings: Settings, tmp_path: Path
) -> None:
    configured = _local_settings(settings, tmp_path, mode="managed")
    manager = LocalBotApiManager(configured)
    executable = configured.telegram.local_bot_api.executable
    assert executable is not None

    command = manager._command(executable)
    rendered = " ".join(command)

    assert "LOCAL_API_HASH_FOR_TESTS" not in rendered
    assert "12345" not in rendered
    assert not any(argument.startswith("--api-id") for argument in command)
    assert not any(argument.startswith("--api-hash") for argument in command)


def test_endpoint_leases_reject_mixed_cloud_and_local_clients(
    settings: Settings, tmp_path: Path
) -> None:
    configured = _local_settings(settings, tmp_path)
    manager = LocalBotApiManager(configured)
    cloud = manager.register_client(role="bot", endpoint="cloud")
    try:
        with pytest.raises(LocalBotApiError):
            manager.register_client(role="worker", endpoint="local")
    finally:
        cloud.close()


def test_current_process_liveness_check_is_non_destructive() -> None:
    assert local_api_module._pid_running(os.getpid())


def test_status_and_external_start_are_safe(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalBotApiManager(_local_settings(settings, tmp_path))
    monkeypatch.setattr(manager, "endpoint_reachable", lambda: True)

    status = manager.status()
    handle = manager.ensure_started()

    assert status.active_endpoint == "cloud"
    assert status.endpoint_reachable
    assert status.process_running
    assert not handle.managed


def test_external_lifecycle_errors_are_explicit(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalBotApiManager(_local_settings(settings, tmp_path))
    monkeypatch.setattr(manager, "endpoint_reachable", lambda: False)

    with pytest.raises(LocalBotApiError):
        manager.ensure_started()
    with pytest.raises(LocalBotApiError):
        manager.start()
    with pytest.raises(LocalBotApiError):
        manager.stop()


def test_active_endpoint_blocks_incomplete_and_disabled_local_state(
    settings: Settings, tmp_path: Path
) -> None:
    configured = _local_settings(settings, tmp_path)
    manager = LocalBotApiManager(configured)
    manager.migration_store.write("local_activation_pending")
    with pytest.raises(LocalBotApiError):
        manager.active_endpoint()

    raw = configured.model_dump()
    raw["telegram"]["local_bot_api"]["enabled"] = False
    disabled = Settings.model_validate(raw)
    disabled_manager = LocalBotApiManager(disabled)
    disabled_manager.migration_store.write("local")
    with pytest.raises(LocalBotApiError):
        disabled_manager.active_endpoint()


def test_migration_store_rejects_invalid_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(LocalBotApiError):
        MigrationStore(path).read()


def test_cloud_wait_is_normalized_and_invalid_timestamp_is_rejected(
    settings: Settings, tmp_path: Path
) -> None:
    manager = LocalBotApiManager(_local_settings(settings, tmp_path))
    manager.migration_store.write(
        "cloud_wait",
        cloud_available_after="2000-01-01T00:00:00+00:00",
    )
    assert manager.active_endpoint() == "cloud"

    manager.migration_store.write("cloud_wait", cloud_available_after="invalid")
    with pytest.raises(LocalBotApiError):
        manager.status()


def test_managed_process_state_roundtrip_and_safe_missing_stop(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = LocalBotApiManager(_local_settings(settings, tmp_path, mode="managed"))
    manager._write_process_state(123)
    assert manager._read_process_pid() == 123
    monkeypatch.setattr(manager, "_managed_pid_matches", lambda _pid: False)

    manager.stop()

    assert manager._read_process_pid() is None


def test_managed_handle_stops_only_once(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = LocalBotApiManager(_local_settings(settings, tmp_path, mode="managed"))
    calls: list[bool] = []
    monkeypatch.setattr(manager, "stop_if_unused", lambda: calls.append(True))
    handle = ManagedLocalApiHandle(manager, managed=True)

    handle.stop_if_owned()
    handle.stop_if_owned()

    assert calls == [True]


async def test_pending_migration_recovers_without_repeating_logout(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = LocalBotApiManager(_local_settings(settings, tmp_path))
    manager.migration_store.write("cloud_logout_pending")

    async def reachable() -> bool:
        return True

    monkeypatch.setattr(manager, "ensure_started", lambda: None)
    monkeypatch.setattr(manager, "_local_bot_reachable", reachable)

    assert (await manager.migrate_to_local()).phase == "local"


async def test_local_activation_and_cloud_migration_fail_closed_when_unreachable(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = LocalBotApiManager(_local_settings(settings, tmp_path))
    manager.migration_store.write("local_activation_pending")

    async def unreachable() -> bool:
        return False

    monkeypatch.setattr(manager, "ensure_started", lambda: None)
    monkeypatch.setattr(manager, "_local_bot_reachable", unreachable)
    with pytest.raises(LocalBotApiError):
        await manager.migrate_to_local()

    manager.migration_store.write("local")
    monkeypatch.setattr(manager, "endpoint_reachable", lambda: False)
    with pytest.raises(LocalBotApiError):
        await manager.migrate_to_cloud()


async def test_bot_method_wrapper_handles_get_me_and_unacknowledged_logout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeSession:
        async def close(self) -> None:
            calls.append("close")

    class FakeBot:
        session = FakeSession()

        async def get_me(self) -> None:
            calls.append("get_me")

        async def log_out(self) -> bool:
            calls.append("logout")
            return False

    monkeypatch.setattr(
        "telegram_media_bot.infrastructure.telegram.local_api.Bot",
        lambda **_kwargs: FakeBot(),
    )

    await local_api_module._call_bot_method(
        token="123456:TEST",
        base_url=None,
        method="get_me",
        is_local=False,
    )
    with pytest.raises(LocalBotApiError):
        await local_api_module._call_bot_method(
            token="123456:TEST",
            base_url=None,
            method="logout",
            is_local=False,
        )

    assert calls == ["get_me", "close", "logout", "close"]


def test_managed_start_passes_credentials_only_in_child_environment(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = _local_settings(settings, tmp_path, mode="managed")
    manager = LocalBotApiManager(configured)
    reachable = iter((False, False, True))
    captured: dict[str, Any] = {}

    class FakeProcess:
        pid = 987654

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def terminate() -> None:
            return None

    def popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured["command"] = command
        captured.update(kwargs)
        captured["env"] = kwargs["env"].copy()
        return FakeProcess()

    monkeypatch.setattr(manager, "endpoint_reachable", lambda: next(reachable))
    monkeypatch.setattr(subprocess, "Popen", popen)

    handle = manager.start()

    assert handle.managed
    assert captured["env"]["TELEGRAM_API_ID"] == "12345"
    assert captured["env"]["TELEGRAM_API_HASH"] == "LOCAL_API_HASH_FOR_TESTS"
    rendered = " ".join(captured["command"])
    assert "LOCAL_API_HASH_FOR_TESTS" not in rendered
    assert str(os.getpid()) not in rendered
