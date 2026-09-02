"""
Handle image uploads for UI/screenshot analysis

Features:
- OCR for text extraction
- UI element detection
- Image description
- Diagram analysis
"""

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Union

from telegram import PhotoSize

from src.config import Settings


def save_image_bytes(data: bytes, ext: str, dest_dir: "Union[str, Path]") -> Path:
    """Write image bytes to a uniquely-named file in dest_dir; return its Path."""
    import uuid

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    safe_ext = (ext or "png").lstrip(".")
    path = dest / f"tg_image_{uuid.uuid4().hex[:12]}.{safe_ext}"
    path.write_bytes(data)
    return path


@dataclass
class ProcessedImage:
    """Processed image result"""

    prompt: str
    image_type: str
    base64_data: str
    size: int
    metadata: Dict[str, any] = None


class ImageHandler:
    """Process image uploads"""

    def __init__(self, config: Settings):
        self.config = config
        self.supported_formats = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

    async def process_image(
        self, photo: PhotoSize, caption: Optional[str] = None
    ) -> ProcessedImage:
        """Process uploaded image"""

        # Download image
        file = await photo.get_file()
        image_bytes = await file.download_as_bytearray()

        # _detect_image_type is a stub that always says "screenshot", so the
        # branchy prompts below sent EVERY photo — including a plate of food
        # for the coach's nutrition log — wrapped in a UI-analysis scaffold
        # ("identify the application, understand the UI elements..."). That
        # framing measurably pulled agents toward describing instead of acting
        # (3 of the wife's clearest missed nutrition-log writes were photo
        # turns, 2026-08-04 research). Use the neutral prompt for everything:
        # the caption and the profile's persona carry the intent, not us.
        image_type = self._detect_image_type(image_bytes)
        prompt = self._create_generic_prompt(caption)

        # Convert to base64 for Claude (if supported in future)
        base64_image = base64.b64encode(image_bytes).decode("utf-8")

        return ProcessedImage(
            prompt=prompt,
            image_type=image_type,
            base64_data=base64_image,
            size=len(image_bytes),
            metadata={
                "format": self._detect_format(image_bytes),
                "has_caption": caption is not None,
            },
        )

    def _detect_image_type(self, image_bytes: bytes) -> str:
        """Detect type of image"""
        # Simple heuristic based on image characteristics
        # In practice, could use ML model for better detection

        # For now, return generic type
        return "screenshot"

    def _detect_format(self, image_bytes: bytes) -> str:
        """Detect image format from magic bytes"""
        # Check magic bytes for common formats
        if image_bytes.startswith(b"\x89PNG"):
            return "png"
        elif image_bytes.startswith(b"\xff\xd8\xff"):
            return "jpeg"
        elif image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
            return "gif"
        elif image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:12]:
            return "webp"
        else:
            return "unknown"

    def _create_generic_prompt(self, caption: Optional[str]) -> str:
        """Neutral photo prompt: the caption IS the request.

        Deliberately does not suggest any task ("analyze", "identify",
        "insights") — the receiving profile's persona decides what a photo
        means (the coach logs a meal, devops reads a screenshot). A
        captionless photo still says only what happened, not what to do.
        """
        if caption:
            return f"I'm sending you a photo. {caption}"
        return (
            "I'm sending you a photo with no caption. Interpret and handle it "
            "according to your role."
        )

    def supports_format(self, filename: str) -> bool:
        """Check if image format is supported"""
        if not filename:
            return False

        # Extract extension
        parts = filename.lower().split(".")
        if len(parts) < 2:
            return False

        extension = f".{parts[-1]}"
        return extension in self.supported_formats

    async def validate_image(self, image_bytes: bytes) -> tuple[bool, Optional[str]]:
        """Validate image data"""
        # Check size
        max_size = 10 * 1024 * 1024  # 10MB
        if len(image_bytes) > max_size:
            return False, "Image too large (max 10MB)"

        # Check format
        format_type = self._detect_format(image_bytes)
        if format_type == "unknown":
            return False, "Unsupported image format"

        # Basic validity check
        if len(image_bytes) < 100:  # Too small to be a real image
            return False, "Invalid image data"

        return True, None
