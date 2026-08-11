from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from time import monotonic

from telegram_media_bot.domain.errors import (
    CollectionTooLargeError,
    GalleryDlExtractionError,
    GalleryDlOutputChangedError,
    JobCancelledError,
)
from telegram_media_bot.infrastructure.gallerydl.models import GalleryProcessResult


class _OutputLimitExceeded(Exception):
    pass


class GalleryDlRunner:
    def __init__(
        self, *, stdout_limit: int = 8 * 1024 * 1024, stderr_limit: int = 256 * 1024
    ) -> None:
        self._stdout_limit = stdout_limit
        self._stderr_limit = stderr_limit

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        is_cancelled: Callable[[], bool] | None = None,
        output_directory: Path | None = None,
        max_output_bytes: int | None = None,
    ) -> GalleryProcessResult:
        return asyncio.run(
            self.run_async(
                args,
                timeout_seconds=timeout_seconds,
                is_cancelled=is_cancelled,
                output_directory=output_directory,
                max_output_bytes=max_output_bytes,
            )
        )

    async def run_async(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        is_cancelled: Callable[[], bool] | None = None,
        output_directory: Path | None = None,
        max_output_bytes: int | None = None,
    ) -> GalleryProcessResult:
        if not args:
            raise GalleryDlExtractionError("gallery-dl command is empty")
        started = monotonic()
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
        except OSError as exc:
            raise GalleryDlExtractionError("Unable to start gallery-dl") from exc
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(_read_bounded(process.stdout, self._stdout_limit))
        stderr_task = asyncio.create_task(_read_bounded(process.stderr, self._stderr_limit))
        wait_task = asyncio.create_task(process.wait())
        try:
            async with asyncio.timeout(timeout_seconds):
                while not wait_task.done():
                    if is_cancelled is not None and is_cancelled():
                        raise JobCancelledError("Gallery download was cancelled")
                    if (
                        output_directory is not None
                        and max_output_bytes is not None
                        and _workspace_size(output_directory) > max_output_bytes
                    ):
                        raise CollectionTooLargeError(
                            "Gallery transfer exceeded the configured total size"
                        )
                    for task in (stdout_task, stderr_task):
                        if task.done() and task.exception() is not None:
                            raise task.exception()  # type: ignore[misc]
                    await asyncio.sleep(0.1)
                return_code = await wait_task
                stdout = await stdout_task
                stderr = await stderr_task
        except TimeoutError as exc:
            await _terminate(process)
            raise GalleryDlExtractionError("gallery-dl timed out") from exc
        except _OutputLimitExceeded as exc:
            await _terminate(process)
            raise GalleryDlOutputChangedError(
                "gallery-dl output exceeded its safety limit"
            ) from exc
        except BaseException:
            await _terminate(process)
            raise
        finally:
            if not stdout_task.done():
                stdout_task.cancel()
            if not stderr_task.done():
                stderr_task.cancel()
            if not wait_task.done():
                wait_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, wait_task, return_exceptions=True)
        return GalleryProcessResult(
            return_code=return_code,
            stdout=stdout,
            stderr=stderr,
            elapsed_seconds=monotonic() - started,
        )


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(64 * 1024):
        size += len(chunk)
        if size > limit:
            raise _OutputLimitExceeded
        chunks.append(chunk)
    return b"".join(chunks)


def _workspace_size(workspace: Path) -> int:
    total = 0
    if not workspace.is_dir():
        return total
    for candidate in workspace.iterdir():
        if candidate.is_symlink() or candidate.is_dir():
            raise GalleryDlOutputChangedError("gallery-dl created an unexpected workspace entry")
        if candidate.is_file():
            total += candidate.stat().st_size
    return total


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        else:
            kill_process_group = os.killpg  # type: ignore[attr-defined]
            kill_process_group(process.pid, signal.SIGTERM)
        await asyncio.wait_for(process.wait(), timeout=5)
        return
    except OSError, ProcessLookupError, TimeoutError:
        pass
    if os.name == "nt":
        with suppress(OSError, subprocess.SubprocessError):
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
    if process.returncode is None:
        process.kill()
    with suppress(ProcessLookupError, TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=10)
