from pathlib import Path
from zipfile import ZipFile

import pytest
from PIL import Image

from telegram_media_bot.bootstrap.config import ImageValidationSection
from telegram_media_bot.domain.errors import ImageValidationError, JobCancelledError
from telegram_media_bot.infrastructure.archive.ordered_zip import OrderedZipBuilder
from telegram_media_bot.infrastructure.image_validation import validate_image


def test_image_validation_uses_signature_not_extension(tmp_path: Path) -> None:
    path = tmp_path / "pretends-to-be.png"
    Image.new("RGB", (32, 24)).save(path, format="JPEG")

    assert validate_image(path, ImageValidationSection()) == "jpeg"


@pytest.mark.parametrize("content", [b"", b"<html>not an image</html>", b"broken"])
def test_image_validation_rejects_empty_html_and_malformed(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "bad.jpg"
    path.write_bytes(content)

    with pytest.raises(ImageValidationError):
        validate_image(path, ImageValidationSection())


def test_image_validation_enforces_pixel_limit(tmp_path: Path) -> None:
    path = tmp_path / "large.png"
    Image.new("RGB", (2000, 1000)).save(path)

    with pytest.raises(ImageValidationError):
        validate_image(path, ImageValidationSection(max_pixels=1_000_000))


def test_ordered_zip_is_deterministic_and_preserves_numeric_order(tmp_path: Path) -> None:
    paths = []
    for name, content in (("z.jpg", b"z"), ("a.jpg", b"a")):
        path = tmp_path / name
        path.write_bytes(content)
        paths.append(path)

    destination = OrderedZipBuilder().build(paths, tmp_path / "original-images.zip")

    with ZipFile(destination) as archive:
        assert archive.namelist() == ["0001-z.jpg", "0002-a.jpg"]
        assert archive.read("0001-z.jpg") == b"z"


def test_ordered_zip_cancellation_removes_partial_archive(tmp_path: Path) -> None:
    source = tmp_path / "one.jpg"
    source.write_bytes(b"image")
    destination = tmp_path / "original-images.zip"

    with pytest.raises(JobCancelledError):
        OrderedZipBuilder().build([source], destination, is_cancelled=lambda: True)

    assert not destination.exists()
