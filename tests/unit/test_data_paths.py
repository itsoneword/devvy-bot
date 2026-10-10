"""DEVVY_DATA_ROOT path derivation (dv-c66a).

Three contracts:
- root unset -> every path is exactly what it was before the setting existed
  (prod runs on that until the migration, dv-4030, flips it);
- root set -> db/, secrets/, workspaces/ under it (no state/, dv-ebde;
  taskpump files live in workspaces/devops/taskpump/); explicit per-path settings still win;
- profiles.yaml: relative paths resolve under <root>/workspaces when the root
  is set (legacy: the config file's dir); absolute paths never move.
"""

from pathlib import Path

import pytest

from src.config import Settings
from src.profiles.registry import load_profile_registry
from src.utils.constants import DEFAULT_DATABASE_URL, DEFAULT_USER_DATABASE_URL
from src.utils.paths import (
    DataLayout,
    data_layout,
    read_data_root,
    sqlite_url,
    taskpump_lock_path,
)

# --- read_data_root -------------------------------------------------------


def test_unset_everywhere_is_legacy(tmp_path):
    assert read_data_root(env={}, dotenv=tmp_path / "missing.env") is None


def test_empty_value_is_legacy(tmp_path):
    assert read_data_root(env={"DEVVY_DATA_ROOT": "  "}, dotenv=tmp_path / "x") is None


def test_env_value_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = read_data_root(env={"DEVVY_DATA_ROOT": "~/Devvybot/data"})
    assert root == tmp_path / "Devvybot" / "data"


def test_dotenv_fallback_and_env_wins(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# comment\nOTHER=1\nexport DEVVY_DATA_ROOT='/srv/devvy'  # inline\n",
        encoding="utf-8",
    )
    assert read_data_root(env={}, dotenv=dotenv) == Path("/srv/devvy")
    # An explicit empty env value means "legacy", even with a .env value.
    assert read_data_root(env={"DEVVY_DATA_ROOT": ""}, dotenv=dotenv) is None
    assert read_data_root(env={"DEVVY_DATA_ROOT": "/other"}, dotenv=dotenv) == Path(
        "/other"
    )


# --- DataLayout -----------------------------------------------------------


def test_legacy_layout_matches_todays_paths():
    layout = DataLayout(root=None)
    assert layout.is_legacy
    assert sqlite_url(layout.bot_db) == DEFAULT_DATABASE_URL
    assert sqlite_url(layout.data_db) == DEFAULT_USER_DATABASE_URL
    assert layout.llm_analytics_db == Path("data/llm_analytics.db")
    assert layout.channel_intel_db == Path("data/channel_intel.db")
    assert layout.codex_credentials == Path("data/model_auth/openai_codex.json")
    assert layout.legacy_codex_sessions == Path("data/codex_sessions.json")
    assert layout.consolidation_dir == Path("data/consolidation")
    assert layout.taskpump_state == Path("data/taskpump.json")
    assert layout.legacy_taskpump_lock == Path("data/taskpump.lock")
    assert layout.taskpump_outbox == Path("data/taskpump-outbox")
    assert layout.workspaces_dir is None


def test_legacy_layout_with_repo_anchor():
    """Scripts anchor the legacy dir on the repo instead of the cwd."""
    layout = data_layout(
        env={}, dotenv=Path("/nonexistent"), legacy_dir=Path("/r/data")
    )
    assert layout.bot_db == Path("/r/data/bot.db")
    assert layout.taskpump_state == Path("/r/data/taskpump.json")


def test_root_layout_derives_every_area():
    layout = data_layout(Path("/d"))
    assert not layout.is_legacy
    assert layout.bot_db == Path("/d/db/bot.db")
    assert layout.data_db == Path("/d/db/data.db")
    assert layout.llm_analytics_db == Path("/d/db/llm_analytics.db")
    assert layout.channel_intel_db == Path("/d/db/channel_intel.db")
    assert layout.workspaces_dir == Path("/d/workspaces")
    assert layout.codex_credentials == Path("/d/secrets/model_auth/openai_codex.json")
    assert layout.consolidation_dir == Path(
        "/d/workspaces/devops/reports/consolidation"
    )
    assert layout.taskpump_state == Path("/d/workspaces/devops/taskpump/state.json")
    assert layout.taskpump_outbox == Path("/d/workspaces/devops/taskpump/outbox")
    # Legacy files stay where they are: read-only import source / old lock.
    assert layout.legacy_codex_sessions == Path("data/codex_sessions.json")
    assert layout.legacy_taskpump_lock == Path("data/taskpump.lock")
    assert not hasattr(layout, "state_dir")


def test_taskpump_lock_is_runtime_not_data():
    assert taskpump_lock_path({"XDG_RUNTIME_DIR": "/run/user/1000"}) == Path(
        "/run/user/1000/devvy-taskpump.lock"
    )
    fallback = taskpump_lock_path({})
    assert fallback.parent == Path("/tmp")
    assert fallback.name.startswith("devvy-taskpump-")


# --- Settings -------------------------------------------------------------


def _settings(**kw):
    return Settings(
        _env_file=None,
        telegram_bot_token="t",
        telegram_bot_username="b",
        **kw,
    )


def test_settings_root_unset_keeps_legacy_defaults():
    s = _settings()
    assert s.devvy_data_root is None
    assert s.database_url == DEFAULT_DATABASE_URL
    assert s.user_database_url == DEFAULT_USER_DATABASE_URL
    assert s.codex_session_store_path == Path("data/codex_sessions.json")
    assert s.openai_codex_credentials_path == Path("data/model_auth/openai_codex.json")


def test_settings_root_set_derives_paths(tmp_path):
    s = _settings(devvy_data_root=str(tmp_path))
    assert s.database_url == f"sqlite:///{tmp_path}/db/bot.db"
    assert s.user_database_url == f"sqlite:///{tmp_path}/db/data.db"
    assert s.database_path == tmp_path / "db" / "bot.db"
    # The legacy Codex JSON is an import source, not part of the root.
    assert s.codex_session_store_path == Path("data/codex_sessions.json")
    assert s.openai_codex_credentials_path == (
        tmp_path / "secrets" / "model_auth" / "openai_codex.json"
    )


def test_settings_root_moves_soul_memory_to_devops_workspace(tmp_path):
    # dv-4030: the global activity log / soul.md must not keep writing into
    # the retired data/ tree once the root is set.
    legacy = _settings()
    assert legacy.memory_path.name == "memory" and legacy.memory_path.parent.name == "data"
    s = _settings(devvy_data_root=str(tmp_path))
    assert s.memory_path == tmp_path / "workspaces" / "devops" / "memory"
    assert s.soul_md_path == tmp_path / "workspaces" / "devops" / "soul.md"


def test_read_bot_secret_env_then_secrets_file_then_dotenv(tmp_path):
    from src.utils.paths import read_bot_secret

    dotenv = tmp_path / ".env"
    dotenv.write_text("K_TOKEN=legacy\n")
    assert read_bot_secret("K_TOKEN", env={}, dotenv=dotenv) == "legacy"
    root = tmp_path / "root"
    (root / "secrets").mkdir(parents=True)
    (root / "secrets" / "bot.env").write_text("K_TOKEN='split'\n")
    dotenv.write_text(f"DEVVY_DATA_ROOT={root}\n")
    assert read_bot_secret("K_TOKEN", env={}, dotenv=dotenv) == "split"
    assert read_bot_secret("K_TOKEN", env={"K_TOKEN": "env"}, dotenv=dotenv) == "env"
    assert read_bot_secret("MISSING", env={}, dotenv=dotenv) is None


def test_settings_explicit_path_beats_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///legacy/bot.db")
    s = _settings()
    assert s.devvy_data_root == tmp_path
    assert s.database_url == "sqlite:///legacy/bot.db"  # compat override wins
    assert s.user_database_url == f"sqlite:///{tmp_path}/db/data.db"


def test_settings_blank_env_is_legacy(monkeypatch):
    monkeypatch.setenv("DEVVY_DATA_ROOT", "")
    s = _settings()
    assert s.devvy_data_root is None
    assert s.database_url == DEFAULT_DATABASE_URL


# --- daemons --------------------------------------------------------------


def test_daemon_db_paths_follow_root(tmp_path, monkeypatch):
    from src.channel_intel.config import ServerConfig
    from src.llm_analytics.config import AnalyticsConfig

    for var in ("CHANNEL_INTEL_DB_PATH", "LLM_ANALYTICS_DB_PATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("LLM_ANALYTICS_BOT_DB_PATH", raising=False)

    assert ServerConfig.from_env().db_path == Path("data/channel_intel.db")
    assert AnalyticsConfig.from_env().db_path == Path("data/llm_analytics.db")

    monkeypatch.setenv("DEVVY_DATA_ROOT", str(tmp_path))
    assert ServerConfig.from_env().db_path == tmp_path / "db" / "channel_intel.db"
    a = AnalyticsConfig.from_env()
    assert a.db_path == tmp_path / "db" / "llm_analytics.db"
    assert a.bot_db_path == tmp_path / "db" / "bot.db"

    # The per-daemon variable is still the override (prod channel-intel unit).
    monkeypatch.setenv("CHANNEL_INTEL_DB_PATH", "/x/channel_intel.db")
    assert ServerConfig.from_env().db_path == Path("/x/channel_intel.db")


# --- profile registry -----------------------------------------------------

CFG = """
profiles:
  - slug: coach
    name: Coach
    working_dir: {wd}
    memory_dir: {md}
    persona: {pe}
    default: true
"""


def _cfg(tmp_path: Path, wd: str, md: str, pe: str) -> Path:
    d = tmp_path / "repo" / "config"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "profiles.yaml"
    p.write_text(CFG.format(wd=wd, md=md, pe=pe), encoding="utf-8")
    return p


def test_relative_profile_paths_legacy_resolve_against_config_dir(tmp_path):
    cfg = _cfg(tmp_path, "coach", "coach/memory", "coach/persona.md")
    p = load_profile_registry(cfg, data_root=None).get_by_slug("coach")
    assert p.working_dir == cfg.parent / "coach"
    assert p.persona == cfg.parent / "coach" / "persona.md"


def test_relative_profile_paths_resolve_under_workspaces(tmp_path):
    root = tmp_path / "data"
    cfg = _cfg(tmp_path, "coach", "coach/memory", "coach/persona.md")
    p = load_profile_registry(cfg, data_root=root).get_by_slug("coach")
    assert p.working_dir == root / "workspaces" / "coach"
    assert p.memory_dir == root / "workspaces" / "coach" / "memory"
    assert p.persona == root / "workspaces" / "coach" / "persona.md"


@pytest.mark.parametrize("data_root", [None, "set"])
def test_absolute_profile_paths_never_move(tmp_path, data_root):
    root = tmp_path / "data" if data_root else None
    cfg = _cfg(tmp_path, "/srv/ws/coach", "/srv/ws/coach/memory", "/srv/p.md")
    p = load_profile_registry(cfg, data_root=root).get_by_slug("coach")
    assert p.working_dir == Path("/srv/ws/coach")
    assert p.memory_dir == Path("/srv/ws/coach/memory")
    assert p.persona == Path("/srv/p.md")


def test_workspaces_var_in_both_layouts(tmp_path):
    cfg = _cfg(
        tmp_path,
        "${DEVVY_WORKSPACES}/coach",
        "${DEVVY_WORKSPACES}/coach/memory",
        "${DEVVY_WORKSPACES}/coach/persona.md",
    )
    legacy = load_profile_registry(cfg, data_root=None).get_by_slug("coach")
    # Legacy: exactly what the old ${DEVVY_ROOT}/workspaces templates gave.
    assert legacy.working_dir == tmp_path / "repo" / "workspaces" / "coach"
    root = tmp_path / "data"
    rooted = load_profile_registry(cfg, data_root=root).get_by_slug("coach")
    assert rooted.working_dir == root / "workspaces" / "coach"


def test_data_root_var_only_defined_when_set(tmp_path):
    cfg = _cfg(tmp_path, "${DEVVY_DATA_ROOT}/x", "/m", "/p.md")
    with pytest.raises(ValueError, match="DEVVY_DATA_ROOT"):
        load_profile_registry(cfg, data_root=None)
    root = tmp_path / "data"
    p = load_profile_registry(cfg, data_root=root).get_by_slug("coach")
    assert p.working_dir == root / "x"


def test_registry_reads_root_from_env_by_default(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(root))
    cfg = _cfg(tmp_path, "coach", "coach/memory", "coach/persona.md")
    p = load_profile_registry(cfg).get_by_slug("coach")
    assert p.working_dir == root / "workspaces" / "coach"


def test_shipped_templates_land_in_workspaces(tmp_path):
    """Every shipped profile template resolves its agent files under
    <root>/workspaces/<slug> (owner decision: personas + memory there)."""
    import importlib.util

    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "install_profiles", repo / "scripts" / "install_profiles.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    names = mod.available_templates()
    cfg = tmp_path / "config" / "profiles.yaml"
    cfg.parent.mkdir()
    cfg.write_text(mod.build_config(names), encoding="utf-8")
    root = tmp_path / "data"
    reg = load_profile_registry(cfg, data_root=root)
    for slug in names:
        p = reg.get_by_slug(slug)
        ws = root / "workspaces" / slug
        assert p.memory_dir == ws / "memory", slug
        assert p.persona == ws / "persona.md", slug
        if slug != "devops":  # devops' working_dir is the checkout itself
            assert p.working_dir == ws, slug
