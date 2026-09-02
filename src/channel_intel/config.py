import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


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
        db_path = Path(os.getenv("CHANNEL_INTEL_DB_PATH", "data/channel_intel.db"))
        token = os.getenv("CHANNEL_INTEL_TOKEN")
        return cls(host=host, port=port, db_path=db_path, token=token)
