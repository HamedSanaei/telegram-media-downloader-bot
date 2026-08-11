from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from telegram_media_bot.domain.errors import JobCancelledError, PostProcessingError


class OrderedZipBuilder:
    """Build one deterministic store-only ZIP inside the existing archive subsystem."""

    def build(
        self,
        files: Sequence[Path],
        destination: Path,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Path:
        if not files:
            raise PostProcessingError("An image ZIP requires at least one file")
        root = destination.parent.resolve()
        if not destination.resolve().is_relative_to(root):
            raise PostProcessingError("ZIP destination escapes the job workspace")
        try:
            with ZipFile(destination, "w", compression=ZIP_STORED, allowZip64=True) as archive:
                for index, path in enumerate(files, start=1):
                    if is_cancelled is not None and is_cancelled():
                        raise JobCancelledError("Image ZIP creation was cancelled")
                    resolved = path.resolve()
                    if (
                        not resolved.is_relative_to(root)
                        or not resolved.is_file()
                        or path.is_symlink()
                    ):
                        raise PostProcessingError("ZIP source escapes the job workspace")
                    info = ZipInfo(f"{index:04d}-{path.name}", date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = ZIP_STORED
                    info.external_attr = 0o100600 << 16
                    with path.open("rb") as source, archive.open(info, "w") as target:
                        while chunk := source.read(1024 * 1024):
                            if is_cancelled is not None and is_cancelled():
                                raise JobCancelledError("Image ZIP creation was cancelled")
                            target.write(chunk)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return destination
