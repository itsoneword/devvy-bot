"""Which STT provider is usable, and with which key — one resolver (dv-6b6c).

Three places used to answer "is voice available" with their own copy of the
provider -> key check (FeatureRegistry, FeatureFlags, the "unavailable" reply).
They all ask here now, and here is the only place that knows a key can come
from the tool key store (``/setkey <provider> <PROVIDER>_API_KEY …``) as well
as from the secrets file.

The key store wins: it is newer, explicit and set at runtime by the owner.
The keystore service is the provider name and the key name is the env name,
so ``/setkey elevenlabs ELEVENLABS_API_KEY …`` reads as what it does.
"""

from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from src.security.keystore import KeyStore

    from .settings import Settings

# provider -> env / keystore key name. Local whisper needs none.
VOICE_KEY_NAMES: Dict[str, str] = {
    "elevenlabs": "ELEVENLABS_API_KEY",
    "openai": "OPENAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}
VOICE_PROVIDERS = ("elevenlabs", "openai", "mistral", "whisper")


def _settings_key(settings: "Settings", provider: str) -> Optional[str]:
    if provider == "openai":
        return settings.openai_api_key_str
    if provider == "elevenlabs":
        return settings.elevenlabs_api_key_str
    if provider == "mistral":
        return settings.mistral_api_key_str
    return None


def voice_api_key(
    settings: "Settings",
    provider: Optional[str] = None,
    keystore: Optional["KeyStore"] = None,
) -> Optional[str]:
    """The API key for ``provider`` (default: the configured one), or None.

    Whisper is keyless and always returns None — use :func:`voice_available`
    for "can this provider run".
    """
    provider = provider or settings.voice_provider
    name = VOICE_KEY_NAMES.get(provider)
    if name is None:
        return None
    if keystore is not None:
        stored = keystore.env(provider).get(name)
        if stored:
            return stored
    return _settings_key(settings, provider)


def voice_provider_ready(
    settings: "Settings",
    provider: Optional[str] = None,
    keystore: Optional["KeyStore"] = None,
) -> bool:
    """True when ``provider`` has what it needs to run (a key, or none needed)."""
    provider = provider or settings.voice_provider
    if provider == "whisper":
        return True
    return voice_api_key(settings, provider, keystore) is not None


def voice_available(
    settings: "Settings", keystore: Optional["KeyStore"] = None
) -> bool:
    """Voice is switched on and the configured provider is ready."""
    return bool(settings.enable_voice_messages) and voice_provider_ready(
        settings, keystore=keystore
    )


def boot_keystore() -> Optional["KeyStore"]:
    """The live key store, or None (legacy layout / unreadable root)."""
    try:
        from src.security.keystore import default_keystore

        return default_keystore()
    except Exception:
        return None
