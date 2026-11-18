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


class TestSendReview:
    @staticmethod
    def _sections(img: Path) -> list:
        return [
            {
                "heading": "h",
                "layout": "grid",
                "items": [{"image": str(img), "caption": "c"}] * 2,
            }
        ]

    @staticmethod
    def _png(path: Path) -> Path:
        from PIL import Image

        Image.new("RGB", (40, 60), (10, 200, 10)).save(path)
        return path

    async def test_renders_pdf_and_cover(self, tmp_path: Path) -> None:
        from src.mcp.telegram_server import send_review

        img = self._png(tmp_path / "a.png")
        out = tmp_path / "rev.pdf"
        result = await send_review("T", self._sections(img), str(out))
        assert "Review queued for delivery" in result
        assert out.read_bytes().startswith(b"%PDF")
        assert (tmp_path / "rev-sheet.png").exists()

    async def test_path_must_be_an_absolute_pdf(self, tmp_path: Path) -> None:
        from src.mcp.telegram_server import send_review

        img = self._png(tmp_path / "a.png")
        assert "Error" in await send_review("T", self._sections(img), "rev.pdf")
        assert "Error" in await send_review(
            "T", self._sections(img), str(tmp_path / "rev.txt")
        )

    async def test_output_outside_roots_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.mcp.telegram_server import send_review

        root = tmp_path / "ws"
        root.mkdir()
        monkeypatch.setenv("TELEGRAM_IMAGE_ROOTS", str(root))
        img = self._png(root / "a.png")
        out = tmp_path / "outside.pdf"
        result = await send_review("T", self._sections(img), str(out))
        assert "outside the deliverable roots" in result
        assert not out.exists()

    async def test_input_image_outside_roots_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.mcp.telegram_server import send_review

        root = tmp_path / "ws"
        root.mkdir()
        monkeypatch.setenv("TELEGRAM_IMAGE_ROOTS", str(root))
        stray = self._png(tmp_path / "stray.png")
        out = root / "rev.pdf"
        result = await send_review("T", self._sections(stray), str(out))
        assert "outside the deliverable roots" in result
        assert not out.exists()

    async def test_missing_relative_and_bad_spec_refused(self, tmp_path: Path) -> None:
        from src.mcp.telegram_server import send_review

        out = str(tmp_path / "rev.pdf")
        gone = [{"layout": "grid", "items": [{"image": str(tmp_path / "x.png")}]}]
        assert "not found" in await send_review("T", gone, out)
        rel = [{"layout": "grid", "items": [{"image": "x.png"}]}]
        assert "absolute" in await send_review("T", rel, out)
        assert "layout" in await send_review(
            "T", [{"layout": "wall", "items": [{"caption": "x"}]}], out
        )

    async def test_bot_delivers_cover_then_pdf(self, tmp_path: Path) -> None:
        from src.bot.utils.image_extractor import (
            attachments_for_tool_call,
            is_send_file_call,
        )

        call = {
            "name": "mcp__telegram__send_review",
            "input": {"file_path": str(tmp_path / "rev.pdf")},
        }
        assert is_send_file_call(call)
        # before the tool ran (the stream intercept): bounds only
        pending = attachments_for_tool_call(call, tmp_path, pending=True)
        assert [a.path.name for a in pending] == ["rev-sheet.png", "rev.pdf"]
        assert attachments_for_tool_call(call, tmp_path) == []
        outside = {"name": "send_review", "input": {"file_path": "/etc/rev.pdf"}}
        assert attachments_for_tool_call(outside, tmp_path, pending=True) == []
