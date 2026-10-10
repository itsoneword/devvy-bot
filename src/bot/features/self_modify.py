"""Self-modification guard for the bot.

Classifies file modifications into tiers:
- Tier 0: Not a self-modification (file outside bot root)
- Tier 1: Safe self-modification (soul, memory, config files) -- no approval needed
- Tier 2: Code self-modification (source, deps) -- needs approval + restart
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Tier 1: files the bot can freely edit (no restart needed). `data/` is the
# legacy data dir; with DEVVY_DATA_ROOT set the data root sits outside the
# checkout, so its files are tier 0 here (dv-2c1c).
_TIER1_PREFIXES = [
    "data/",
    "config/",
]
_TIER1_FILES = [
    "CLAUDE.md",
]

# Tier 2: files that need approval and trigger a restart
_TIER2_PREFIXES = [
    "src/",
    "tests/",
    "deploy/",
]
_TIER2_FILES = [
    # Bot secrets: agents cannot read or write them at all (denylist); if
    # anything routes a change here it needs the owner.
    ".env",
    "pyproject.toml",
    "poetry.lock",
    "install.sh",
    "Makefile",
]


class SelfModificationGuard:
    """Classifies and gates modifications to the bot's own files."""

    def __init__(self, bot_root: Path) -> None:
        self.bot_root = bot_root.resolve()

    def get_tier(self, file_path: str) -> int:
        """Classify a file path into a modification tier.

        Returns:
            0 = not a self-modification
            1 = safe (soul, memory, config)
            2 = code change (source, deps)
        """
        resolved = Path(file_path).resolve()
        # Key material and the archive (dv-2c1c) need the owner wherever the
        # data root lives, inside the checkout (legacy data/model_auth) or not.
        from src.security.denylist import protected_dirs

        for area in protected_dirs().everyone:
            if resolved == area or area in resolved.parents:
                return 2
        try:
            relative = str(resolved.relative_to(self.bot_root))
        except ValueError:
            return 0  # outside bot root

        for prefix in _TIER1_PREFIXES:
            if relative.startswith(prefix):
                return 1

        if relative in _TIER1_FILES:
            return 1

        for prefix in _TIER2_PREFIXES:
            if relative.startswith(prefix):
                return 2

        if relative in _TIER2_FILES:
            return 2

        # Other files in bot root (e.g. README.md) -- treat as tier 1
        return 1

    def needs_approval(self, file_path: str) -> bool:
        """Whether this file modification needs user approval via Telegram."""
        return self.get_tier(file_path) == 2

    def needs_restart(self, file_path: str) -> bool:
        """Whether modifying this file requires a bot restart to take effect."""
        return self.get_tier(file_path) == 2

    async def trigger_restart(self, delay_seconds: float = 1.0) -> None:
        """Restart the bot service via systemctl.

        The delay allows the calling code to send a Telegram message before
        the process dies.
        """
        logger.info("Self-restart triggered", delay=delay_seconds)
        await asyncio.sleep(delay_seconds)
        subprocess.Popen(
            ["sudo", "systemctl", "restart", "claude-bot"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
