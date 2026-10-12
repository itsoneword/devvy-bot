"""
Central feature registry and management
"""

from typing import Any, Dict, Optional

import structlog

from src.config.settings import Settings
from src.config.voice import boot_keystore, voice_api_key, voice_available
from src.security.keystore import KeyStore
from src.security.validators import SecurityValidator
from src.storage.facade import Storage

from .file_handler import FileHandler
from .image_handler import ImageHandler
from .voice_handler import VoiceHandler

logger = structlog.get_logger(__name__)


class FeatureRegistry:
    """Manage all bot features"""

    def __init__(
        self,
        config: Settings,
        storage: Storage,
        security: SecurityValidator,
        keystore: Optional[KeyStore] = None,
    ):
        self.config = config
        self.storage = storage
        self.security = security
        # Voice keys set with /setkey live here; boot must see them too, or a
        # restart silently drops a provider chosen through /voice (dv-6b6c).
        self.keystore = keystore if keystore is not None else boot_keystore()
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
        if voice_available(self.config, self.keystore):
            try:
                self.features["voice_handler"] = VoiceHandler(
                    config=self.config,
                    api_key=voice_api_key(self.config, keystore=self.keystore),
                )
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

    def set_voice_handler(self, handler: Optional[VoiceHandler]) -> None:
        """Swap the live voice handler (None = voice off) — no restart.

        Handlers read ``features`` per message, so the next voice note uses
        the new one. The caller closes the old handler's client.
        """
        if handler is None:
            self.features.pop("voice_handler", None)
        else:
            self.features["voice_handler"] = handler
        logger.info("Voice handler replaced", enabled=handler is not None)

    def get_enabled_features(self) -> Dict[str, Any]:
        """Get all enabled features"""
        return self.features.copy()

    def shutdown(self):
        """Shutdown all features"""
        logger.info("Shutting down features")

        # Clear feature registry
        self.features.clear()

        logger.info("Feature shutdown complete")
