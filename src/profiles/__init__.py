"""Profile registry: per-persona working dir, memory, and identity."""

from .registry import (
    ProfileDefinition,
    ProfileRegistry,
    load_profile_registry,
    validate_registry_paths,
)

__all__ = [
    "ProfileDefinition",
    "ProfileRegistry",
    "load_profile_registry",
    "validate_registry_paths",
]
