import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.utils.paths import data_layout, read_bot_secret


@dataclass
class ServerConfig:
    host: str
    port: int
    db_path: Path
    token: Optional[str]

    @classmethod
    def from_env(cls) -> "ServerConfig":
        host = os.getenv("CHANNEL_INTEL_HOST", "127.0.0.1")
        port = int(os.getenv("CHANNEL_INTEL_PORT", "8765"))
        # Unset DEVVY_DATA_ROOT = legacy data/channel_intel.db (dv-c66a).
        db_path = Path(
            os.getenv("CHANNEL_INTEL_DB_PATH") or str(data_layout().channel_intel_db)
        )
        # Env first (legacy EnvironmentFile=.env), else <root>/secrets/bot.env:
        # after the secrets split the unit's EnvironmentFile holds config only.
        token = read_bot_secret("CHANNEL_INTEL_TOKEN")
        return cls(host=host, port=port, db_path=db_path, token=token)
