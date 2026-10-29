"""One data root, every runtime path derived from it (dv-c66a, epic dv-fcab).

``DEVVY_DATA_ROOT`` names the directory that holds everything the bot writes
at runtime, outside the git checkout::

    <root>/db/          bot.db, data.db, llm_analytics.db, channel_intel.db
    <root>/workspaces/  one dir per agent: persona.md, memory/, users/, guests/
    <root>/secrets/     model_auth/ (Codex OAuth), later bot env + MCP tokens
    <root>/grafana/     devvy.db: the restricted export Grafana mounts (dv-e005)

There is no ``state/`` area (owner decision 2026-09-26, dv-ebde): Codex
thread bookkeeping and pinned status messages live in bot.db, consolidation
reports and the taskpump counter/outbox under the devops workspace
(``<root>/workspaces/devops/``), and the taskpump lock in
``$XDG_RUNTIME_DIR`` (see :func:`taskpump_lock_path`).

The intended value is :data:`DEFAULT_DATA_ROOT` (``~/Devvybot/data``), but it
is **not** applied implicitly. Unset means *legacy layout*: every path is what
it was before this module existed (``data/bot.db`` relative to the repo, and
so on), so a deployment that has not migrated keeps booting on its current
files. The migration (dv-4030) moves the data and then sets the variable.

Standard library only: stdlib scripts (``scripts/taskpump``) import this, and
the llm_analytics / channel_intel daemons read it without loading Settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Tuple

DATA_ROOT_VAR = "DEVVY_DATA_ROOT"
# The archive bin (dv-3099): unused files, kept findable, never agent-readable.
# Optional override; by default it sits next to the data root (see
# :func:`archive_dir`).
ARCHIVE_DIR_VAR = "DEVVY_ARCHIVE_DIR"
# Documented target, used by the migration and .env.example. Deliberately not
# a silent fallback: see the module docstring.
DEFAULT_DATA_ROOT = "~/Devvybot/data"

# The checkout this module lives in (src/utils/paths.py -> repo root).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# Legacy layout: everything flat under data/, resolved against the process
# cwd (the bot and its daemons run with WorkingDirectory = the repo).
LEGACY_DATA_DIR = Path("data")


def _clean(value: Optional[str]) -> Optional[Path]:
    if value is None:
        return None
    value = value.strip().strip("'\"")
    if not value:
        return None
    return Path(os.path.expandvars(value)).expanduser()


def _dotenv_value(dotenv: Path, key: str) -> Optional[str]:
    """``key``'s value in a KEY=VALUE .env file, or None. No interpolation."""
    try:
        text = dotenv.read_text(encoding="utf-8")
    except OSError:
        return None
    found: Optional[str] = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            found = value.split(" #", 1)[0]
    return found


def read_data_root(
    env: Optional[Mapping[str, str]] = None,
    dotenv: Optional[Path] = None,
) -> Optional[Path]:
    """The configured data root, or None for the legacy layout.

    Process environment wins; otherwise the repo's ``.env`` (the same file
    pydantic Settings reads), so a daemon or script sees the value the bot
    sees without each one growing its own EnvironmentFile line.
    """
    env = os.environ if env is None else env
    if DATA_ROOT_VAR in env:
        return _clean(env[DATA_ROOT_VAR])
    dotenv = REPO_ROOT / ".env" if dotenv is None else dotenv
    return _clean(_dotenv_value(dotenv, DATA_ROOT_VAR))


@dataclass(frozen=True)
class DataLayout:
    """Every runtime path, for one data root (or the legacy layout)."""

    root: Optional[Path]
    legacy_dir: Path = LEGACY_DATA_DIR

    @property
    def is_legacy(self) -> bool:
        return self.root is None

    def _area(self, name: str) -> Path:
        return self.legacy_dir if self.root is None else self.root / name

    # --- areas ----------------------------------------------------------

    @property
    def db_dir(self) -> Path:
        return self._area("db")

    @property
    def secrets_dir(self) -> Path:
        return self._area("secrets")

    @property
    def grafana_dir(self) -> Path:
        """``<root>/grafana``: the only data dir Grafana mounts (read-only).
        Holds the restricted usage export, never a DB of the bot (dv-e005)."""
        return self._area("grafana")

    @property
    def workspaces_dir(self) -> Optional[Path]:
        """``<root>/workspaces``. None in the legacy layout: there, workspace
        paths are whatever profiles.yaml says (absolute, or relative to the
        config file) — see ``src/profiles/registry.py``."""
        return None if self.root is None else self.root / "workspaces"

    @property
    def secret_dirs(self) -> Tuple[Path, ...]:
        """Directories that hold key material, denied to EVERY agent
        (dv-2c1c). Root mode: ``<root>/secrets``. Legacy: ``data/model_auth``
        (Codex OAuth) only — ``secrets_dir`` there is the whole ``data/``, and
        the legacy ``.env`` is covered by the denylist's filename patterns."""
        if self.root is None:
            return (self.legacy_dir / "model_auth",)
        return (self.secrets_dir,)

    @property
    def tenant_db_dir(self) -> Optional[Path]:
        """``<root>/db``: every SQLite store, denied to sandboxed agents. None
        in the legacy layout, where ``data/`` also holds non-DB files and the
        denylist matches the DB basenames instead."""
        return None if self.root is None else self.db_dir

    # --- databases ------------------------------------------------------

    @property
    def bot_db(self) -> Path:
        return self.db_dir / "bot.db"

    @property
    def data_db(self) -> Path:
        return self.db_dir / "data.db"

    @property
    def llm_analytics_db(self) -> Path:
        return self.db_dir / "llm_analytics.db"

    @property
    def channel_intel_db(self) -> Path:
        return self.db_dir / "channel_intel.db"

    # --- secrets --------------------------------------------------------

    @property
    def bot_secrets_file(self) -> Optional[Path]:
        """The bot's secrets (KEY=VALUE, 0600): ``<root>/secrets/bot.env``.

        None in the legacy layout, where they sit in the repo ``.env`` with
        the config. Read into Settings only, never exported (dv-2976)."""
        return None if self.root is None else self.secrets_dir / "bot.env"

    @property
    def codex_credentials(self) -> Path:
        return self.secrets_dir / "model_auth" / "openai_codex.json"

    # --- devops workspace ------------------------------------------------

    @property
    def devops_workspace(self) -> Optional[Path]:
        """``<root>/workspaces/devops``: the admin agent's memory, soul.md and
        persona (its working_dir stays the repo). None in the legacy layout,
        where they sit flat in ``data/``."""
        return None if self.root is None else self.root / "workspaces" / "devops"

    # --- workspace-owned output ---------------------------------------

    @property
    def consolidation_dir(self) -> Path:
        """Consolidation reports: the devops agent reviews them, so they live
        in its workspace. Legacy layout keeps ``data/consolidation``."""
        if self.root is None:
            return self.legacy_dir / "consolidation"
        return self.root / "workspaces" / "devops" / "reports" / "consolidation"

    # --- taskpump ---------------------------------------------------------

    @property
    def taskpump_state(self) -> Path:
        if self.root is None:
            return self.legacy_dir / "taskpump.json"
        return self.root / "workspaces" / "devops" / "taskpump" / "state.json"

    @property
    def taskpump_outbox(self) -> Path:
        if self.root is None:
            return self.legacy_dir / "taskpump-outbox"
        return self.root / "workspaces" / "devops" / "taskpump" / "outbox"

    # --- model availability ---------------------------------------------

    @property
    def model_quota(self) -> Path:
        """The runtime model-availability record (dv-7ad9, dv-d63d): the last
        usage-limit hit and the owner's job switch (``job_route``), read before
        every model job. Each writer rewrites only its own keys
        (``src/model_router/quota.py``)."""
        if self.root is None:
            return self.legacy_dir / "model_quota.json"
        return self.root / "workspaces" / "devops" / "model_quota.json"

    # --- legacy files, read-only ------------------------------------------

    @property
    def legacy_codex_sessions(self) -> Path:
        """The pre-dv-ebde Codex JSON store. Only read, by the one-shot import
        into the ``sessions`` table (src/storage/codex_sessions.py); never
        written. Always under the legacy dir: it is not part of the new root."""
        return self.legacy_dir / "codex_sessions.json"

    @property
    def legacy_taskpump_lock(self) -> Path:
        """Where taskpump locked before dv-ebde. A tick still takes it (if the
        file exists) so a pre-merge tick and a post-merge tick cannot overlap."""
        return self.legacy_dir / "taskpump.lock"


def read_bot_secret(
    name: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    dotenv: Optional[Path] = None,
) -> Optional[str]:
    """One secret for a stdlib consumer (agentctl, the daemons): the process
    env, else ``<root>/secrets/bot.env`` (root mode), else the repo ``.env``
    (legacy layout keeps secrets there). Never logs or exports the value."""
    env = os.environ if env is None else env
    value = (env.get(name) or "").strip()
    if value:
        return value
    dotenv = REPO_ROOT / ".env" if dotenv is None else dotenv
    secrets_file = data_layout(env=env, dotenv=dotenv).bot_secrets_file
    for source in (secrets_file, dotenv):
        if source is None:
            continue
        found = _dotenv_value(source, name)
        if found is not None:
            found = found.strip().strip("'\"")
            if found:
                return found
    return None


def archive_dir(
    layout: Optional[DataLayout] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    dotenv: Optional[Path] = None,
) -> Path:
    """The archive bin: ``DEVVY_ARCHIVE_DIR`` (env, then repo ``.env``), else
    ``archive/`` next to the data root, else (legacy layout) next to the
    checkout. For the documented install both give ``~/Devvybot/archive``."""
    env = os.environ if env is None else env
    if ARCHIVE_DIR_VAR in env:
        configured = _clean(env[ARCHIVE_DIR_VAR])
    else:
        dotenv = REPO_ROOT / ".env" if dotenv is None else dotenv
        configured = _clean(_dotenv_value(dotenv, ARCHIVE_DIR_VAR))
    if configured is not None:
        return configured
    if layout is None:
        layout = data_layout(env=env, dotenv=dotenv)
    base = layout.root.parent if layout.root is not None else REPO_ROOT.parent
    return base / "archive"


def bot_claude_md_excludes(
    *,
    env: Optional[Mapping[str, str]] = None,
    dotenv: Optional[Path] = None,
) -> Tuple[str, ...]:
    """CLAUDE.md files no bot chat or job may load (dv-fe02, owner 3.1).

    The CLI walks up from the cwd and loads every CLAUDE.md it finds, plus the
    owner's ``~/.claude/CLAUDE.md``, even with ``setting_sources=["project"]``:
    a chat in ``<root>/workspaces/chef`` got the owner's CLI style rules and
    the install dir's notes (4082 chars, docs/FINDINGS.md 2026-10-25). Passed
    as the CLI's ``claudeMdExcludes`` setting by both option builders. Only
    these two absolute paths: a working dir's own CLAUDE.md still loads.

    Derived, never hardcoded: ``$HOME`` and the install dir, which is the
    data root's parent and, in the documented layout, also the checkout's
    parent (``~/Devvybot`` for both); both are named, so the legacy layout
    (no data root) is covered too.
    """
    root = read_data_root(env, dotenv)
    found = [str(Path.home() / ".claude" / "CLAUDE.md")]
    for install in (root.parent if root is not None else None, REPO_ROOT.parent):
        if install is not None and str(install / "CLAUDE.md") not in found:
            found.append(str(install / "CLAUDE.md"))
    return tuple(found)


def taskpump_lock_path(env: Optional[Mapping[str, str]] = None) -> Path:
    """Tick lock: ``$XDG_RUNTIME_DIR`` (tmpfs, per user, cleared on reboot),
    else a per-uid file in /tmp. Not data: it never belongs in the root."""
    env = os.environ if env is None else env
    runtime = (env.get("XDG_RUNTIME_DIR") or "").strip()
    if runtime:
        return Path(runtime) / "devvy-taskpump.lock"
    return Path("/tmp") / f"devvy-taskpump-{os.getuid()}.lock"


def sqlite_url(path: Path) -> str:
    """``sqlite:///<path>`` in the form Settings.database_url expects."""
    return f"sqlite:///{path}"


def data_layout(
    root: Optional[Path] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    dotenv: Optional[Path] = None,
    legacy_dir: Path = LEGACY_DATA_DIR,
) -> DataLayout:
    """Layout for an explicit ``root``, else for :func:`read_data_root`."""
    if root is None:
        root = read_data_root(env, dotenv)
    else:
        root = Path(root).expanduser()
    return DataLayout(root=root, legacy_dir=legacy_dir)
