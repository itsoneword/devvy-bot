from pathlib import Path
from types import SimpleNamespace

from src.bot.features.image_handler import save_image_bytes
from src.bot.orchestration.formatting import image_save_extension


def test_save_image_bytes_writes_file(tmp_path):
    p = save_image_bytes(b"\x89PNG\r\n", "png", dest_dir=tmp_path)
    assert Path(p).exists()
    assert Path(p).read_bytes().startswith(b"\x89PNG")
    assert str(p).endswith(".png")


def test_image_save_extension_uses_detected_format_not_image_type():
    """Regression test: agentic_photo used to pass ProcessedImage.image_type
    (a prompt-selection category — "screenshot", "diagram", "ui_mockup" —
    and currently a stub that always returns "screenshot") as the saved
    file's extension. Files ended up named "tg_image_xxx.screenshot", which
    downstream backends (Claude's Read tool, Codex's -i) don't recognize as
    an image by extension, so a perfectly valid JPEG got reported back to
    the user as an unreadable binary file. The extension must come from the
    actual detected format instead."""
    processed = SimpleNamespace(
        image_type="screenshot",
        metadata={"format": "jpeg", "has_caption": False},
    )
    assert image_save_extension(processed) == "jpeg"


def test_image_save_extension_handles_png():
    processed = SimpleNamespace(
        image_type="screenshot", metadata={"format": "png", "has_caption": False}
    )
    assert image_save_extension(processed) == "png"


def test_image_save_extension_falls_back_for_unknown_format():
    processed = SimpleNamespace(
        image_type="screenshot", metadata={"format": "unknown", "has_caption": False}
    )
    assert image_save_extension(processed) == "jpeg"


def test_image_save_extension_handles_missing_metadata():
    processed = SimpleNamespace(image_type="screenshot", metadata=None)
    assert image_save_extension(processed) == "jpeg"
