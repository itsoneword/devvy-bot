"""
Central feature registry and management
"""

from typing import Any, Dict, Optional

import structlog

from src.config.settings import Settings
from src.security.validators import SecurityValidator
from src.storage.facade import Storage

from .file_handler import FileHandler
from .image_handler import ImageHandler
from .voice_handler import VoiceHandler

logger = structlog.get_logger(__name__)


class FeatureRegistry:
    """Manage all bot features"""

    def __init__(self, config: Settings, storage: Storage, security: SecurityValidator):
        self.config = config
        self.storage = storage
        self.security = security
        self.features: Dict[str, Any] = {}

        # Initialize features based on config
        self._initialize_features()

    def _initialize_features(self):
        """Initialize enabled features"""
        logger.info("Initializing bot features")

        # File upload handling - conditionally enabled
        if self.config.enable_file_uploads:
            try:
                self.features["file_handler"] = FileHandler(
                    config=self.config, security=self.security
                )
                logger.info("File handler feature enabled")
            except Exception as e:
                logger.error("Failed to initialize file handler", error=str(e))

        # Image handling - always enabled
        try:
            self.features["image_handler"] = ImageHandler(config=self.config)
            logger.info("Image handler feature enabled")
        except Exception as e:
            logger.error("Failed to initialize image handler", error=str(e))

        # Voice transcription - requires provider-specific API key (whisper is keyless)
        voice_key_available = (
            (self.config.voice_provider == "whisper")
            or (self.config.voice_provider == "openai" and self.config.openai_api_key)
            or (self.config.voice_provider == "mistral" and self.config.mistral_api_key)
            or (
                self.config.voice_provider == "elevenlabs"
                and self.config.elevenlabs_api_key
            )
        )
        if self.config.enable_voice_messages and voice_key_available:
            try:
                self.features["voice_handler"] = VoiceHandler(config=self.config)
                logger.info("Voice handler feature enabled")
            except Exception as e:
                logger.error("Failed to initialize voice handler", error=str(e))

        logger.info(
            "Feature initialization complete",
            enabled_features=list(self.features.keys()),
        )

    def get_feature(self, name: str) -> Optional[Any]:
        """Get feature by name"""
        return self.features.get(name)

    def is_enabled(self, feature_name: str) -> bool:
        """Check if feature is enabled"""
        return feature_name in self.features

    def get_file_handler(self) -> Optional[FileHandler]:
        """Get file handler feature"""
        return self.get_feature("file_handler")

    def get_image_handler(self) -> Optional[ImageHandler]:
        """Get image handler feature"""
        return self.get_feature("image_handler")

    def get_voice_handler(self) -> Optional[VoiceHandler]:
        """Get voice handler feature"""
        return self.get_feature("voice_handler")

    def get_enabled_features(self) -> Dict[str, Any]:
        """Get all enabled features"""
        return self.features.copy()

    def shutdown(self):
        """Shutdown all features"""
        logger.info("Shutting down features")

        # Clear feature registry
        self.features.clear()

        logger.info("Feature shutdown complete")
