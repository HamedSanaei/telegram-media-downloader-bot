"""Production-like, network-free smoke for yt-dlp inspection scratch-file resolution.

Run inside the real container with a read-only root filesystem and no writable ``/tmp`` to
reproduce the production failure conditions exactly:

.. code-block:: sh

   docker run --rm --network none --read-only \\
       --tmpfs /data:rw,noexec,nosuid,size=64m,mode=1777,uid=10001,gid=10001 \\
       -v "${PWD}/config.example.yaml:/app/config.example.yaml:ro" \\
       <image> python -m telegram_media_bot.infrastructure.ytdlp.inspection_workspace_smoke \\
           --config /app/config.example.yaml

The smoke proves that ``/app`` is read-only, that the configured application storage temp
hierarchy is writable, that yt-dlp inspection options resolve their format-probe scratch
files (``_check_formats``) into that hierarchy instead of the working directory, and that a
full ``YtDlpEngine.inspect`` run succeeds without ever attempting ``/app/tmp*.tmp``.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import tempfile
from pathlib import Path
from typing import Any, ClassVar, cast

from telegram_media_bot.bootstrap.config import Settings, load_settings
from telegram_media_bot.domain.models import MediaKind
from telegram_media_bot.infrastructure.ytdlp import engine as engine_module
from telegram_media_bot.infrastructure.ytdlp.options import (
    YtDlpOptionsFactory,
    remove_inspection_workspace,
)

_INFO: dict[str, Any] = {
    "id": "qRk26ZpZZMQ",
    "title": "Inspection workspace smoke",
    "extractor_key": "Youtube",
    "webpage_url": "https://www.youtube.com/watch?v=qRk26ZpZZMQ",
    "duration": 212,
    "vcodec": "avc1.640028",
    "acodec": "mp4a.40.2",
    "ext": "mp4",
    "formats": [
        {
            "format_id": "140",
            "ext": "m4a",
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "filesize": 3_300_000,
        },
        {
            "format_id": "137",
            "ext": "mp4",
            "vcodec": "avc1.640028",
            "acodec": "none",
            "height": 1080,
            "width": 1920,
            "fps": 30,
            "filesize": 61_000_000,
        },
    ],
}


class _ScratchProbeYoutubeDL:
    """Offline stand-in replicating yt-dlp's inspection-time filesystem behavior."""

    info: ClassVar[dict[str, Any]] = _INFO
    used_temp_dir: ClassVar[str | None] = None

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        self.format_selector = self._last_format

    def _last_format(self, context: dict[str, Any]) -> Any:
        return iter(context["formats"][-1:])

    def __enter__(self) -> _ScratchProbeYoutubeDL:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def build_format_selector(self, selector: str) -> Any:
        if selector.startswith("bestaudio"):
            return lambda context: iter(
                item for item in reversed(context["formats"]) if item["acodec"] != "none"
            )

        def video_audio(context: dict[str, Any]) -> Any:
            videos = [item for item in context["formats"] if item["vcodec"] != "none"]
            audios = [item for item in context["formats"] if item["acodec"] != "none"]
            if not videos or not audios:
                return iter(())
            return iter(({"requested_formats": [videos[-1], audios[-1]]},))

        return video_audio

    def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
        del download
        paths = self.options.get("paths") or {}
        home = str(paths.get("home") or "")
        temp = str(paths.get("temp") or "")
        resolved = os.path.join(home, temp)
        if not resolved:
            raise RuntimeError("inspection options lost the configured workspace path")
        type(self).used_temp_dir = resolved
        with tempfile.NamedTemporaryFile(suffix=".tmp", delete=False, dir=resolved) as handle:
            handle.write(b"probe")
        return dict(self.info)

    def sanitize_info(self, raw: Any) -> Any:
        return raw


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to the operator config file")
    arguments = parser.parse_args()
    settings: Settings = load_settings(arguments.config)
    # Network-free containers have no DNS resolution, so the extracted-URL private-network
    # check (which resolves hostnames) is disabled for this smoke only. Production keeps it
    # enabled; the workspace behavior under test is independent of it.
    settings = settings.model_copy(
        update={
            "security": settings.security.model_copy(update={"reject_private_network_urls": False})
        }
    )

    # 1. The application filesystem must be read-only, matching production.
    leaked: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tmp", delete=False, dir="/app") as handle:
            handle.write(b"x")
            leaked = Path(handle.name)
    except OSError as error:
        if error.errno not in {errno.EROFS, errno.EACCES, errno.EPERM}:
            raise
    if leaked is not None:
        raise RuntimeError("/app unexpectedly accepted a temporary file")

    # 2. Ambient temp resolution must be unusable, reproducing the production fallback.
    if os.name == "posix":
        try:
            with tempfile.NamedTemporaryFile(suffix=".tmp"):
                raise RuntimeError(
                    "ambient tempdir was writable; conditions are not production-like"
                )
        except OSError:
            pass

    # 3. The configured application temp hierarchy is writable.
    temp_root = settings.storage.temp_path()
    temp_root.mkdir(parents=True, exist_ok=True)
    probe_path = temp_root / ".smoke-probe"
    probe_path.write_bytes(b"ok")
    probe_path.unlink()

    # 4. Inspection options resolve scratch files into the storage hierarchy.
    factory = YtDlpOptionsFactory(settings)
    options = factory.inspect_options()
    paths = options.get("paths") or {}
    workspace = Path(str(paths.get("home") or ""))
    assert paths.get("temp") == str(workspace), "home and temp must both be configured"
    if not workspace.is_relative_to(temp_root):
        raise RuntimeError(f"workspace escaped storage temp root: {workspace}")
    if not workspace.is_dir():
        raise RuntimeError("inspection workspace does not exist before extract_info()")
    remove_inspection_workspace(options)

    # 5. A full engine.inspect run succeeds without touching /app.
    patchable = cast(Any, engine_module)
    original = patchable.YoutubeDL
    patchable.YoutubeDL = _ScratchProbeYoutubeDL
    try:
        info = engine_module.YtDlpEngine(settings).inspect("https://youtu.be/qRk26ZpZZMQ")
    finally:
        patchable.YoutubeDL = original
    if info.source != "youtube" or info.kind is not MediaKind.VIDEO:
        raise RuntimeError("inspection smoke returned unexpected media")
    if _ScratchProbeYoutubeDL.used_temp_dir is None or not Path(
        _ScratchProbeYoutubeDL.used_temp_dir
    ).is_relative_to(temp_root):
        raise RuntimeError("scratch files were not created inside storage temp")
    if list(Path("/app").glob("tmp*.tmp")):
        raise RuntimeError("/app received inspection temp files")
    if any(temp_root.glob("inspect-*")):
        raise RuntimeError("inspection workspace leaked after the run")

    print(
        json.dumps(
            {
                "app_read_only": True,
                "storage_temp_writable": True,
                "scratch_dir_inside_storage_temp": True,
                "engine_inspect_succeeded": True,
                "app_tmp_files": 0,
                "workspace_cleaned_after_run": True,
                "source": info.source,
                "media_kind": info.kind.value,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
