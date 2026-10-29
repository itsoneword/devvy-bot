"""Environment-driven configuration (mirrors src/channel_intel/config.py)."""

import os
from dataclasses import dataclass
from pathlib import Path

from src.utils.paths import REPO_ROOT, data_layout


@dataclass
class AnalyticsConfig:
    host: str
    port: int
    db_path: Path
    projects_dir: Path
    bot_db_path: Path
    pricing_path: Path
    scan_interval: float
    # Session attribution sources (dv-d2a3). None = that source is absent:
    # attribution then falls back to cwd rules and flags nothing archived.
    profiles_path: Path | None = None
    jobs_dir: Path | None = None
    pump_worktrees_dir: Path | None = None
    boards_path: Path | None = None
    # Restricted export for Grafana (dv-e005); None = off.
    grafana_export_path: Path | None = None

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
        # Same env vars and defaults as the bot (PROFILES_CONFIG_PATH),
        # scripts/bgjob (BGJOB_ROOT) and scripts/taskpump (PUMP_WORKTREES).
        profiles_env = os.getenv("PROFILES_CONFIG_PATH")
        profiles_path = Path(profiles_env).expanduser() if profiles_env else None
        jobs_dir = Path(os.getenv("BGJOB_ROOT") or str(REPO_ROOT.parent / "jobs"))
        pump_worktrees_dir = Path(
            os.getenv("PUMP_WORKTREES") or str(REPO_ROOT.parent / "pump-worktrees")
        )
        boards_path = REPO_ROOT / "config" / "taskpump" / "boards.json"
        # Unset = <root>/grafana/devvy.db; set to "" = export off.
        export = os.getenv(
            "LLM_ANALYTICS_GRAFANA_EXPORT", str(layout.grafana_dir / "devvy.db")
        )
        grafana_export_path = Path(export).expanduser() if export else None
        return cls(
            host=host,
            port=port,
            db_path=db_path,
            projects_dir=projects_dir,
            bot_db_path=bot_db_path,
            pricing_path=pricing_path,
            scan_interval=scan_interval,
            profiles_path=profiles_path,
            jobs_dir=jobs_dir,
            pump_worktrees_dir=pump_worktrees_dir,
            boards_path=boards_path,
            grafana_export_path=grafana_export_path,
        )
