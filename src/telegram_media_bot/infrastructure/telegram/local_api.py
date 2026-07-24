from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from contextlib import suppress
from csv import reader as csv_reader
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import LocalBotApiError

MigrationPhase = Literal[
    "cloud",
    "cloud_logout_pending",
    "cloud_logout_uncertain",
    "local_activation_pending",
    "local",
    "local_logout_pending",
    "local_logout_uncertain",
    "cloud_wait",
]


@dataclass(frozen=True, slots=True)
class MigrationState:
    version: int = 1
    phase: MigrationPhase = "cloud"
    updated_at: str = ""
    cloud_available_after: str | None = None


@dataclass(frozen=True, slots=True)
class LocalApiStatus:
    enabled: bool
    mode: str
    process_running: bool
    endpoint_reachable: bool
    migration_phase: MigrationPhase
    active_endpoint: Literal["cloud", "local", "blocked"]


@dataclass(slots=True)
class ManagedLocalApiHandle:
    manager: LocalBotApiManager
    managed: bool

    def stop_if_owned(self) -> None:
        if self.managed:
            self.manager.stop_if_unused()
            self.managed = False


@dataclass(slots=True)
class EndpointLease:
    path: Path

    def close(self) -> None:
        self.path.unlink(missing_ok=True)


class MigrationStore:
    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve()

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> MigrationState:
        if not self._path.exists():
            return MigrationState(updated_at=_now())
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError
            phase = raw.get("phase")
            allowed = {
                "cloud",
                "cloud_logout_pending",
                "cloud_logout_uncertain",
                "local_activation_pending",
                "local",
                "local_logout_pending",
                "local_logout_uncertain",
                "cloud_wait",
            }
            if phase not in allowed:
                raise ValueError
            return MigrationState(
                version=int(raw.get("version", 1)),
                phase=phase,
                updated_at=str(raw.get("updated_at") or ""),
                cloud_available_after=(
                    str(raw["cloud_available_after"]) if raw.get("cloud_available_after") else None
                ),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise LocalBotApiError("Local Bot API migration state is invalid") from exc

    def write(
        self,
        phase: MigrationPhase,
        *,
        cloud_available_after: str | None = None,
    ) -> MigrationState:
        state = MigrationState(
            phase=phase,
            updated_at=_now(),
            cloud_available_after=cloud_available_after,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        try:
            temporary.write_text(
                json.dumps(asdict(state), ensure_ascii=True, sort_keys=True),
                encoding="utf-8",
            )
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, self._path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise LocalBotApiError("Unable to persist Local Bot API migration state") from exc
        return state


class LocalBotApiManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._config = settings.telegram.local_bot_api
        self._store = MigrationStore(self._config.migration.state_file)
        self._process_state = (
            self._config.working_directory.expanduser().resolve() / "managed-process.json"
        )
        self._start_lock = (
            self._config.working_directory.expanduser().resolve() / "managed-start.lock"
        )
        self._leases_directory = self._store.path.parent / "telegram-api-leases"

    @property
    def migration_store(self) -> MigrationStore:
        return self._store

    def register_client(
        self,
        *,
        role: Literal["bot", "worker"],
        endpoint: Literal["cloud", "local"],
    ) -> EndpointLease:
        live_endpoints = {item[1] for item in self._live_leases()}
        if live_endpoints and live_endpoints != {endpoint}:
            raise LocalBotApiError(
                "A Telegram client is already running against a different API endpoint"
            )
        self._leases_directory.mkdir(parents=True, exist_ok=True)
        lease_path = self._leases_directory / f"{role}-{os.getpid()}.json"
        temporary = lease_path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "pid": os.getpid(),
                        "role": role,
                        "endpoint": endpoint,
                        "started_at": _now(),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, lease_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise LocalBotApiError("Unable to register Telegram API client") from exc
        return EndpointLease(lease_path)

    def status(self) -> LocalApiStatus:
        state = self._normalized_state()
        reachable = self.endpoint_reachable()
        process_running = (
            self._managed_process_running() if self._config.mode == "managed" else reachable
        )
        active = _active_endpoint(state)
        return LocalApiStatus(
            enabled=self._config.enabled,
            mode=self._config.mode,
            process_running=process_running,
            endpoint_reachable=reachable,
            migration_phase=state.phase,
            active_endpoint=active,
        )

    def active_endpoint(self) -> Literal["cloud", "local"]:
        state = self._normalized_state()
        endpoint = _active_endpoint(state)
        if endpoint == "blocked":
            raise LocalBotApiError(
                "Telegram API migration is incomplete; use the explicit local-api migration command"
            )
        if endpoint == "local":
            if not self._config.enabled:
                raise LocalBotApiError(
                    "Migration state is local but local_bot_api.enabled is false"
                )
            return "local"
        return "cloud"

    def ensure_started(self) -> ManagedLocalApiHandle:
        if not self._config.enabled:
            raise LocalBotApiError("Local Bot API is disabled")
        if self._config.mode == "external":
            if not self.endpoint_reachable():
                raise LocalBotApiError("External Local Bot API endpoint is unreachable")
            return ManagedLocalApiHandle(self, managed=False)
        if self.endpoint_reachable():
            return ManagedLocalApiHandle(self, managed=True)
        if not self._config.auto_start:
            raise LocalBotApiError("Managed Local Bot API auto_start is disabled")
        return self.start()

    def start(self) -> ManagedLocalApiHandle:
        if not self._config.enabled or self._config.mode != "managed":
            raise LocalBotApiError("Managed Local Bot API is not enabled")
        if self.endpoint_reachable():
            return ManagedLocalApiHandle(self, managed=True)
        lock = self._acquire_start_lock()
        if lock is None:
            return ManagedLocalApiHandle(self, managed=True)
        try:
            return self._start_managed()
        finally:
            os.close(lock)
            self._start_lock.unlink(missing_ok=True)

    def _start_managed(self) -> ManagedLocalApiHandle:
        if self.endpoint_reachable():
            return ManagedLocalApiHandle(self, managed=True)
        executable = self._config.executable
        if executable is None or not executable.expanduser().resolve().is_file():
            raise LocalBotApiError("Managed Local Bot API executable is unavailable")
        self._settings.create_runtime_directories()
        command = self._command(executable.expanduser().resolve())
        child_environment = os.environ.copy()
        child_environment["TELEGRAM_API_ID"] = str(self._config.api_id)
        api_hash = self._config.api_hash
        if api_hash is None:
            raise LocalBotApiError("Managed Local Bot API credentials are unavailable")
        child_environment["TELEGRAM_API_HASH"] = api_hash.get_secret_value()
        log_path = self._config.log_file.expanduser().resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_stream = log_path.open("ab", buffering=0)
        if os.name != "nt":
            log_path.chmod(0o600)
        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            start_new_session = True
        try:
            process = subprocess.Popen(
                command,
                cwd=self._config.working_directory.expanduser().resolve(),
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
        except OSError as exc:
            log_stream.close()
            raise LocalBotApiError("Unable to start managed Local Bot API") from exc
        finally:
            child_environment["TELEGRAM_API_HASH"] = "[REDACTED]"
        log_stream.close()
        self._write_process_state(process.pid)
        deadline = time.monotonic() + self._config.startup_timeout_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._process_state.unlink(missing_ok=True)
                raise LocalBotApiError("Managed Local Bot API exited during startup")
            if self.endpoint_reachable():
                return ManagedLocalApiHandle(self, managed=True)
            time.sleep(0.2)
        process.terminate()
        self._wait_for_exit(process.pid, self._config.shutdown_timeout_seconds)
        self._process_state.unlink(missing_ok=True)
        raise LocalBotApiError("Managed Local Bot API startup timed out")

    def stop_if_unused(self) -> None:
        if self._config.mode == "managed" and not self._live_leases():
            self.stop()

    def stop(self) -> None:
        if self._config.mode != "managed":
            raise LocalBotApiError("External Local Bot API lifecycle is not managed")
        pid = self._read_process_pid()
        if pid is None or not self._managed_pid_matches(pid):
            self._process_state.unlink(missing_ok=True)
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            raise LocalBotApiError("Unable to stop managed Local Bot API") from exc
        if not self._wait_for_exit(pid, self._config.shutdown_timeout_seconds):
            force_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
            with suppress(OSError):
                os.kill(pid, force_signal)
        self._process_state.unlink(missing_ok=True)

    def endpoint_reachable(self) -> bool:
        host, port = self._endpoint_host_port()
        try:
            with socket.create_connection((host, port), timeout=1.5):
                return True
        except OSError:
            return False

    async def migrate_to_local(self) -> MigrationState:
        if not self._config.enabled:
            raise LocalBotApiError("Local Bot API is disabled")
        self._assert_no_live_clients()
        state = self._normalized_state()
        if state.phase == "local":
            return state
        self.ensure_started()
        if state.phase == "cloud":
            self._store.write("cloud_logout_pending")
            try:
                await _call_bot_method(
                    token=self._settings.telegram.token(),
                    base_url=None,
                    method="logout",
                    is_local=False,
                )
            except LocalBotApiError:
                self._store.write("cloud_logout_uncertain")
                raise
            state = self._store.write("local_activation_pending")
        elif state.phase == "cloud_logout_pending":
            if await self._local_bot_reachable():
                return self._store.write("local")
            self._store.write("cloud_logout_uncertain")
            raise LocalBotApiError("Cloud logOut outcome is uncertain; it will not be repeated")
        elif state.phase not in {"local_activation_pending"}:
            raise LocalBotApiError("Migration state does not permit migration to local")
        if not await self._local_bot_reachable():
            raise LocalBotApiError("Local Bot API did not accept the bot after cloud logOut")
        return self._store.write("local")

    async def migrate_to_cloud(self) -> MigrationState:
        self._assert_no_live_clients()
        state = self._normalized_state()
        if state.phase == "cloud":
            return state
        if state.phase == "cloud_wait":
            return state
        if state.phase != "local":
            raise LocalBotApiError("Migration state does not permit migration to cloud")
        if not self.endpoint_reachable():
            raise LocalBotApiError("Local Bot API endpoint is unreachable")
        self._store.write("local_logout_pending")
        try:
            await _call_bot_method(
                token=self._settings.telegram.token(),
                base_url=self._settings.telegram.local_api_base_url,
                method="logout",
                is_local=self._settings.telegram.local_api_is_local,
            )
        except LocalBotApiError:
            self._store.write("local_logout_uncertain")
            raise
        available_after = datetime.now(UTC) + timedelta(minutes=10)
        state = self._store.write(
            "cloud_wait",
            cloud_available_after=available_after.isoformat(),
        )
        if self._config.mode == "managed":
            self.stop()
        return state

    async def _local_bot_reachable(self) -> bool:
        try:
            await _call_bot_method(
                token=self._settings.telegram.token(),
                base_url=self._settings.telegram.local_api_base_url,
                method="get_me",
                is_local=self._settings.telegram.local_api_is_local,
            )
        except LocalBotApiError:
            return False
        return True

    def _normalized_state(self) -> MigrationState:
        state = self._store.read()
        if state.phase != "cloud_wait" or state.cloud_available_after is None:
            return state
        try:
            available = datetime.fromisoformat(state.cloud_available_after)
        except ValueError as exc:
            raise LocalBotApiError("Cloud migration availability time is invalid") from exc
        if datetime.now(UTC) >= available:
            return self._store.write("cloud")
        return state

    def _endpoint_host_port(self) -> tuple[str, int]:
        base_url = self._settings.telegram.local_api_base_url
        if self._config.mode == "managed" or base_url is None:
            return self._config.host, self._config.port
        parsed = urlsplit(base_url)
        if parsed.hostname is None:
            raise LocalBotApiError("Local Bot API URL has no host")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.hostname, port

    def _command(self, executable: Path) -> list[str]:
        command = [
            str(executable),
            f"--http-ip-address={self._config.host}",
            f"--http-port={self._config.port}",
            f"--dir={self._config.working_directory.expanduser().resolve()}",
            f"--temp-dir={self._config.temp_directory.expanduser().resolve()}",
            f"--log={self._config.log_file.expanduser().resolve()}",
            f"--verbosity={self._config.verbosity}",
        ]
        if self._config.local_mode:
            command.append("--local")
        return command

    def _write_process_state(self, pid: int) -> None:
        self._process_state.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._process_state.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": pid,
                    "executable": (
                        self._config.executable.name if self._config.executable is not None else ""
                    ),
                    "started_at": _now(),
                }
            ),
            encoding="utf-8",
        )
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, self._process_state)

    def _read_process_pid(self) -> int | None:
        try:
            raw = json.loads(self._process_state.read_text(encoding="utf-8"))
            pid = raw.get("pid") if isinstance(raw, dict) else None
            return int(pid) if isinstance(pid, int) and pid > 0 else None
        except OSError, ValueError, TypeError, json.JSONDecodeError:
            return None

    def _managed_process_running(self) -> bool:
        pid = self._read_process_pid()
        return pid is not None and self._managed_pid_matches(pid)

    def _managed_pid_matches(self, pid: int) -> bool:
        executable = self._config.executable
        if executable is None:
            return False
        return _pid_matches_executable(pid, executable.expanduser().resolve())

    def _acquire_start_lock(self) -> int | None:
        self._start_lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._config.startup_timeout_seconds
        while time.monotonic() < deadline:
            try:
                descriptor = os.open(
                    self._start_lock,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.write(descriptor, str(os.getpid()).encode("ascii"))
                return descriptor
            except FileExistsError:
                if self.endpoint_reachable():
                    return None
                with suppress(OSError):
                    age = time.time() - self._start_lock.stat().st_mtime
                    if age > self._config.startup_timeout_seconds:
                        self._start_lock.unlink()
                time.sleep(0.2)
            except OSError as exc:
                raise LocalBotApiError("Unable to acquire managed Local Bot API lock") from exc
        raise LocalBotApiError("Timed out waiting for managed Local Bot API start lock")

    def _assert_no_live_clients(self) -> None:
        if self._live_leases():
            raise LocalBotApiError(
                "Stop the bot and worker before changing the Telegram API endpoint"
            )

    def _live_leases(
        self,
    ) -> list[tuple[int, Literal["cloud", "local"]]]:
        if not self._leases_directory.exists():
            return []
        live: list[tuple[int, Literal["cloud", "local"]]] = []
        for path in self._leases_directory.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError
                pid = raw.get("pid")
                endpoint = raw.get("endpoint")
                if not isinstance(pid, int) or pid <= 0 or endpoint not in {"cloud", "local"}:
                    raise ValueError
                if not _pid_running(pid):
                    path.unlink(missing_ok=True)
                    continue
                live.append((pid, cast(Literal["cloud", "local"], endpoint)))
            except OSError, ValueError, TypeError, json.JSONDecodeError:
                path.unlink(missing_ok=True)
        return live

    def _wait_for_exit(self, pid: int, timeout_seconds: int) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not self._managed_pid_matches(pid):
                return True
            time.sleep(0.2)
        return not self._managed_pid_matches(pid)


def effective_settings(settings: Settings, endpoint: Literal["cloud", "local"]) -> Settings:
    if endpoint == "local":
        return settings
    raw = settings.model_dump()
    raw["telegram"]["max_upload_size_mb"] = min(settings.telegram.max_upload_size_mb, 50)
    return Settings.model_validate(raw)


def _active_endpoint(state: MigrationState) -> Literal["cloud", "local", "blocked"]:
    if state.phase == "local":
        return "local"
    if state.phase == "cloud":
        return "cloud"
    return "blocked"


async def _call_bot_method(
    *,
    token: str,
    base_url: str | None,
    method: Literal["logout", "get_me"],
    is_local: bool,
) -> None:
    session: AiohttpSession | None = None
    if base_url is not None:
        api = TelegramAPIServer.from_base(base_url.rstrip("/"), is_local=is_local)
        session = AiohttpSession(api=api)
    bot = Bot(token=token, session=session)
    try:
        if method == "logout":
            result = await bot.log_out()
            if result is not True:
                raise LocalBotApiError("Telegram logOut was not acknowledged")
        else:
            await bot.get_me()
    except LocalBotApiError:
        raise
    except Exception as exc:
        raise LocalBotApiError("Telegram API migration request failed") from exc
    finally:
        await bot.session.close()


def _pid_running(pid: int) -> bool:
    if os.name == "nt":
        try:
            completed = subprocess.run(
                [
                    "tasklist.exe",
                    "/FI",
                    f"PID eq {pid}",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
        except OSError, subprocess.SubprocessError:
            return False
        return completed.returncode == 0 and f'"{pid}"' in completed.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _pid_matches_executable(pid: int, executable: Path) -> bool:
    if os.name == "nt":
        try:
            completed = subprocess.run(
                [
                    "tasklist.exe",
                    "/FI",
                    f"PID eq {pid}",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
        except OSError, subprocess.SubprocessError:
            return False
        rows = list(csv_reader(completed.stdout.splitlines()))
        return bool(
            completed.returncode == 0
            and rows
            and rows[0]
            and rows[0][0].casefold() == executable.name.casefold()
        )
    proc_executable = Path("/proc") / str(pid) / "exe"
    try:
        return proc_executable.resolve(strict=True) == executable.resolve(strict=True)
    except OSError:
        return False


def _now() -> str:
    return datetime.now(UTC).isoformat()
