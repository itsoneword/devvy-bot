"""Tests for the Telegram MCP server tool functions."""

from pathlib import Path

import pytest

from src.bot.utils.image_extractor import validate_image_path
from src.mcp.telegram_server import send_file_to_user, send_image_to_user


@pytest.fixture
def image_file(tmp_path: Path) -> Path:
    """Create a sample image file."""
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    return img


class TestSendImageToUser:
    async def test_valid_image(self, image_file: Path) -> None:
        result = await send_image_to_user(str(image_file))
        assert "Image queued for delivery" in result
        assert "chart.png" in result

    async def test_valid_image_with_caption(self, image_file: Path) -> None:
        result = await send_image_to_user(str(image_file), caption="My chart")
        assert "Image queued for delivery" in result

    async def test_relative_path_rejected(self, image_file: Path) -> None:
        result = await send_image_to_user("relative/path/chart.png")
        assert "Error" in result
        assert "absolute" in result

    async def test_missing_file_rejected(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.png"
        result = await send_image_to_user(str(missing))
        assert "Error" in result
        assert "not found" in result

    async def test_non_image_extension_rejected(self, tmp_path: Path) -> None:
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("hello")
        result = await send_image_to_user(str(txt_file))
        assert "Error" in result
        assert "unsupported" in result

    async def test_all_supported_extensions(self, tmp_path: Path) -> None:
        for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"]:
            img = tmp_path / f"test{ext}"
            img.write_bytes(b"\x00" * 10)
            result = await send_image_to_user(str(img))
            assert "Image queued for delivery" in result, f"Failed for {ext}"

    async def test_case_insensitive_extension(self, tmp_path: Path) -> None:
        img = tmp_path / "photo.JPG"
        img.write_bytes(b"\x00" * 10)
        result = await send_image_to_user(str(img))
        assert "Image queued for delivery" in result


class TestSendFileToUser:
    async def test_pdf_queued(self, tmp_path: Path) -> None:
        pdf = tmp_path / "cv.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        assert "File queued for delivery" in await send_file_to_user(str(pdf))

    async def test_outside_roots_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pdf = tmp_path / "cv.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        monkeypatch.setenv("TELEGRAM_IMAGE_ROOTS", str(tmp_path / "ws"))
        assert "NOT sent" in await send_file_to_user(str(pdf))

    def test_validator_accepts_any_file_only_when_asked(self, tmp_path: Path) -> None:
        pdf = tmp_path / "cv.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        assert validate_image_path(str(pdf), tmp_path) is None
        att = validate_image_path(str(pdf), tmp_path, any_file=True)
        assert att is not None and att.mime_type == "application/pdf"
