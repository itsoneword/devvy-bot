"""Environment-driven configuration (mirrors src/channel_intel/config.py)."""

import os
from dataclasses import dataclass
from pathlib import Path

from src.utils.paths import data_layout


@dataclass
class AnalyticsConfig:
    host: str
    port: int
    db_path: Path
    projects_dir: Path
    bot_db_path: Path
    pricing_path: Path
    scan_interval: float

    @classmethod
    def from_env(cls) -> "AnalyticsConfig":
        host = os.getenv("LLM_ANALYTICS_HOST", "127.0.0.1")
        port = int(os.getenv("LLM_ANALYTICS_PORT", "4790"))
        # Unset DEVVY_DATA_ROOT = legacy data/*.db (dv-c66a).
        layout = data_layout()
        db_path = Path(
            os.getenv("LLM_ANALYTICS_DB_PATH") or str(layout.llm_analytics_db)
        )
        projects_dir = Path(
            os.getenv("LLM_ANALYTICS_PROJECTS_DIR", "~/.claude/projects")
        ).expanduser()
        bot_db_path = Path(os.getenv("LLM_ANALYTICS_BOT_DB_PATH") or str(layout.bot_db))
        pricing_path = Path(
            os.getenv("LLM_ANALYTICS_PRICING_PATH", "config/llm_pricing.json")
        )
        scan_interval = float(os.getenv("LLM_ANALYTICS_SCAN_INTERVAL", "30"))
        return cls(
            host=host,
            port=port,
            db_path=db_path,
            projects_dir=projects_dir,
            bot_db_path=bot_db_path,
            pricing_path=pricing_path,
            scan_interval=scan_interval,
        )
