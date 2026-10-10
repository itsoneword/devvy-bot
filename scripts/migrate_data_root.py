#!/usr/bin/env python3
"""Move the legacy data layout under one data root (dv-4030, epic dv-fcab).

    poetry run python scripts/migrate_data_root.py            # dry run (default)
    poetry run python scripts/migrate_data_root.py --apply    # copy + switch config

Legacy layout (DEVVY_DATA_ROOT unset)        New layout (src/utils/paths.py)
  devvy_bot/data/*.db (+ -wal/-shm)       ->  <root>/db/*.db  (sqlite backup API)
  ~/Devvybot/workspaces/<agent>/          ->  <root>/workspaces/<agent>/
  data/{agent.md,<slug>/agent.md}         ->  <root>/workspaces/<slug>/persona.md
  data/memory, soul.md, user.md           ->  <root>/workspaces/devops/...
  data/consolidation                      ->  workspaces/devops/reports/consolidation
  data/taskpump.json, taskpump-outbox     ->  workspaces/devops/taskpump/{state.json,outbox}
  data/model_auth/                        ->  <root>/secrets/model_auth/  (0700/0600)
  .env secret keys                        ->  <root>/secrets/bot.env      (0600)
  codex_sessions.json, pinned_messages... ->  <archive>/<stamp>/...      (already in bot.db)

``--apply`` COPIES, never moves: the old tree stays intact for rollback. It
refuses while the bot (or a writer daemon) is running, verifies each DB copy
with ``PRAGMA integrity_check`` plus per-table row counts, rewrites workspace
paths stored in the new bot.db, then rewrites ``.env`` (secrets out,
DEVVY_DATA_ROOT in) and ``config/profiles.yaml`` (relative paths). Pristine
copies of both go to the archive first and every later run plans from them,
so a re-run before cutover redoes the copy from scratch; after a completed
run (marker file in the root) it only reports.

Claude SDK sessions start fresh (owner, 2026-09-27; DECISIONS.md): their
~/.claude/projects dirs are not renamed and the moved Claude session rows are
deactivated, so the next turn cold-starts with a seed from the DB. Codex rows
keep resuming by thread id; only their project_path changes.

Secret values are never printed: only key names.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.utils.agent_env import is_secret_name  # noqa: E402
from src.utils.paths import DATA_ROOT_VAR, DEFAULT_DATA_ROOT  # noqa: E402

TICKET = "dv-4030"
MARKER = ".migrated-dv-4030.json"
DB_NAMES = ("bot.db", "data.db", "llm_analytics.db", "channel_intel.db")
DB_SIDECARS = ("-wal", "-shm")
# Env vars that would override the derived paths. The migration relies on
# derivation, so any of these left in .env is a blocker, not a warning.
OVERRIDE_VARS = (
    "DATABASE_URL",
    "USER_DATABASE_URL",
    "CODEX_SESSION_STORE_PATH",
    "OPENAI_CODEX_CREDENTIALS_PATH",
    "LLM_ANALYTICS_DB_PATH",
    "LLM_ANALYTICS_BOT_DB_PATH",
    "CHANNEL_INTEL_DB_PATH",
    "MEMORY_PATH",
    "SOUL_MD_PATH",
)
# data/ entries and where they go. Anything not listed (and not a DB, a
# profile persona/memory or a <slug>/ persona dir) is UNHANDLED and blocks
# --apply until classified.
STAY_IN_REPO = {
    "templates": "committed templates (code)",
    "shared-capabilities.md": "committed, loaded by PromptComposer (code)",
    "taskpump.lock": "legacy lock, still taken by taskpump if present",
}
TO_DEVOPS = {
    "soul.md": "soul.md",
    "user.md": "user.md",
    "consolidation": "reports/consolidation",
    "taskpump.json": "taskpump/state.json",
    "taskpump-outbox": "taskpump/outbox",
}
TO_ARCHIVE = {
    "codex_sessions.json": "imported into bot.db (dv-ebde)",
    "pinned_messages.json": "imported into bot.db (dv-2a61)",
    "tmp": "devops scratch, no code refs",
}
ARCHIVE_PATTERNS = (
    (re.compile(r"^bot\.db\.bak-"), "write-only DB backup"),
    (re.compile(r"\.log$"), "one-off script log"),
)
# Tables whose path columns point at workspaces. window_bundles is left
# alone on purpose: a bundle is a frozen system prompt keyed by working_dir;
# the new dir misses and re-materialises one with the new paths.
PATH_COLUMNS = (
    ("sessions", "project_path"),
    ("conversation_windows", "working_dir"),
    ("pending_notes", "working_dir"),
    ("scheduled_jobs", "working_directory"),
)
SERVICES_SYSTEM = (
    "claude-bot.service",
    "llm-analytics.service",
    "channel-intel.service",
)
SERVICES_USER = ("taskpump.timer", "taskpump.service")


# --- plan model --------------------------------------------------------------


@dataclass
class Item:
    kind: str  # db | tree | file | archive | secret
    src: Path
    dst: Path
    size: int
    note: str = ""


@dataclass
class SqlUpdate:
    table: str
    column: str
    old: str
    new: str
    rows: int


@dataclass
class Plan:
    repo: Path
    root: Path
    old_workspaces: Path
    archive: Path
    items: List[Item] = field(default_factory=list)
    stays: List[Tuple[Path, str]] = field(default_factory=list)
    unhandled: List[Path] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    secret_keys: List[str] = field(default_factory=list)
    config_keys: List[str] = field(default_factory=list)
    env_text: str = ""  # new .env content (config only, no secret lines)
    secrets_text: str = ""  # new secrets/bot.env content
    env_source: Optional[Path] = None
    profiles_source: Optional[Path] = None
    profiles_text: str = ""
    profile_changes: List[Tuple[str, str, str, str]] = field(default_factory=list)
    dir_map: Dict[str, str] = field(default_factory=dict)  # old abs -> new abs
    sql: List[SqlUpdate] = field(default_factory=list)
    claude_deactivate: int = 0
    unmatched_paths: List[Tuple[str, str, int]] = field(default_factory=list)
    already_applied: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(i.size for i in self.items)


# --- helpers -----------------------------------------------------------------


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def tree_size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for name in filenames + [
            d for d in dirnames if os.path.islink(os.path.join(dirpath, d))
        ]:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def ro_uri(path: Path) -> str:
    """Read-only URI that never creates files in the old tree. With a -wal
    present (live or crashed writer) plain mode=ro reads it; without one the
    DB is fully checkpointed and immutable=1 stops SQLite from creating
    -wal/-shm sidecars next to it."""
    if Path(f"{path}-wal").exists():
        return f"file:{path}?mode=ro"
    return f"file:{path}?mode=ro&immutable=1"


def db_size(path: Path) -> int:
    return sum(
        p.stat().st_size
        for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if p.exists()
    )


def parse_env_lines(text: str) -> List[Tuple[Optional[str], str]]:
    """[(key or None, raw line)] preserving comments/blank lines."""
    out: List[Tuple[Optional[str], str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            out.append((None, raw))
        else:
            out.append((line.split("=", 1)[0].strip(), raw))
    return out


def split_env(text: str, root: Path) -> Tuple[str, str, List[str], List[str]]:
    """(config_text, secrets_text, secret_keys, config_keys).

    Secret lines (``is_secret_name``) move to the secrets file; everything
    else, comments included, stays. DEVVY_DATA_ROOT is set (replaced if
    present) so the bot, daemons and scripts all derive the new layout."""
    config_lines: List[str] = []
    secret_lines: List[str] = [
        f"# Bot secrets ({TICKET}). Read by the bot loader only; never an",
        "# EnvironmentFile. Mode 0600.",
    ]
    secret_keys: List[str] = []
    config_keys: List[str] = []
    root_line = f"{DATA_ROOT_VAR}={root}"
    root_written = False
    for key, raw in parse_env_lines(text):
        if key is None:
            config_lines.append(raw)
        elif key == DATA_ROOT_VAR:
            if not root_written:
                config_lines.append(root_line)
                root_written = True
                config_keys.append(key)
        elif is_secret_name(key):
            secret_lines.append(raw)
            if key not in secret_keys:
                secret_keys.append(key)
        else:
            config_lines.append(raw)
            if key not in config_keys:
                config_keys.append(key)
    if not root_written:
        config_lines += [
            "",
            f"# One data root ({TICKET}): db/, workspaces/, secrets/ derive from it.",
            root_line,
        ]
        config_keys.append(DATA_ROOT_VAR)
    return (
        "\n".join(config_lines) + "\n",
        "\n".join(secret_lines) + "\n",
        secret_keys,
        config_keys,
    )


def _under(path: Path, base: Path) -> Optional[Path]:
    try:
        return path.relative_to(base)
    except ValueError:
        return None


_PROFILE_KEY_RE = re.compile(
    r"^(?P<lead>\s*(?:-\s+)?(?P<key>working_dir|memory_dir|persona)\s*:\s*)"
    r"(?P<q>[\"']?)(?P<val>[^\"'#\s]+)(?P=q)(?P<tail>\s*(?:#.*)?)$"
)
_SLUG_RE = re.compile(r"^\s*-\s+slug\s*:\s*[\"']?(?P<slug>[A-Za-z0-9_-]+)[\"']?")


def rewrite_profiles(
    text: str, *, repo: Path, old_workspaces: Path
) -> Tuple[str, List[Tuple[str, str, str, str]], Dict[str, List[Tuple[str, Path]]]]:
    """Rewrite profile paths to workspace-relative form, line by line so the
    comments survive. Returns (new_text, changes, moves) where changes are
    (slug, key, old, new) and moves maps slug -> [(key, legacy source)] for
    persona/memory files that live in the repo's data/ dir.

    Rules: under ~/Devvybot/workspaces -> relative; persona under data/ ->
    <slug>/persona.md; memory_dir under data/ -> <slug>/memory. Anything else
    (the devops working_dir = the repo, mcp seams, extra_dirs) is untouched.
    """
    data_dir = repo / "data"
    slug: Optional[str] = None
    out: List[str] = []
    changes: List[Tuple[str, str, str, str]] = []
    moves: Dict[str, List[Tuple[str, Path]]] = {}
    for line in text.splitlines():
        m_slug = _SLUG_RE.match(line)
        if m_slug:
            slug = m_slug.group("slug")
        m = _PROFILE_KEY_RE.match(line)
        if m and slug:
            key, val = m.group("key"), m.group("val")
            p = Path(os.path.expanduser(val))
            new: Optional[str] = None
            rel_ws = _under(p, old_workspaces) if p.is_absolute() else None
            rel_data = _under(p, data_dir) if p.is_absolute() else None
            if rel_ws is not None:
                new = rel_ws.as_posix()
            elif rel_data is not None and key == "persona":
                new = f"{slug}/persona.md"
                moves.setdefault(slug, []).append((key, p))
            elif rel_data is not None and key == "memory_dir":
                new = f"{slug}/memory"
                moves.setdefault(slug, []).append((key, p))
            if new is not None and new != val:
                changes.append((slug, key, val, new))
                q = m.group("q")
                line = f"{m.group('lead')}{q}{new}{q}{m.group('tail')}"
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), changes, moves


# --- planning ----------------------------------------------------------------


def _pristine(path: Path, archive_stamp: Path, repo: Path) -> Path:
    """The pre-migration copy of a config file if a previous run saved one."""
    saved = archive_stamp / "pre-migration" / path.relative_to(repo)
    return saved if saved.exists() else path


def build_plan(
    *,
    repo: Path,
    root: Path,
    old_workspaces: Path,
    archive: Path,
    env_file: Optional[Path] = None,
    profiles_file: Optional[Path] = None,
    bot_db: Optional[Path] = None,
) -> Plan:
    """Pure-ish planning: reads the old tree (and bot.db read-only), writes
    nothing. ``bot_db`` defaults to <repo>/data/bot.db."""
    data = repo / "data"
    env_file = env_file or repo / ".env"
    profiles_file = profiles_file or repo / "config" / "profiles.yaml"
    plan = Plan(repo=repo, root=root, old_workspaces=old_workspaces, archive=archive)
    ws_new = root / "workspaces"
    devops = ws_new / "devops"

    if (root / MARKER).exists():
        plan.already_applied = True

    # Root must be empty, or hold only what a previous (unfinished) run made.
    if root.exists():
        extra = sorted(
            p.name
            for p in root.iterdir()
            if p.name not in ("db", "workspaces", "secrets", MARKER)
        )
        if extra:
            plan.blockers.append(f"{root} holds unexpected entries: {', '.join(extra)}")

    # --- profiles.yaml (decides persona/memory moves) -------------------
    plan.profiles_source = _pristine(profiles_file, archive, repo)
    claimed: set = set()
    if plan.profiles_source.exists():
        text = plan.profiles_source.read_text(encoding="utf-8")
        plan.profiles_text, plan.profile_changes, moves = rewrite_profiles(
            text, repo=repo, old_workspaces=old_workspaces
        )
        for slug, entries in moves.items():
            for key, src in entries:
                rel = src.relative_to(data)
                claimed.add(rel.parts[0])
                if key == "persona":
                    dst = ws_new / slug / "persona.md"
                    note = f"persona of '{slug}'"
                else:
                    dst = ws_new / slug / "memory"
                    note = f"memory of '{slug}'"
                if not src.exists():
                    plan.warnings.append(f"{note}: source missing: {src}")
                    continue
                plan.items.append(
                    Item(
                        "tree" if src.is_dir() else "file",
                        src,
                        dst,
                        tree_size(src),
                        note,
                    )
                )
                # Other files next to a per-slug persona (data/coach/evidence.md)
                # belong to that agent's workspace too.
                if len(rel.parts) == 2 and key == "persona":
                    for sib in sorted(src.parent.iterdir()):
                        if sib != src:
                            plan.items.append(
                                Item(
                                    "file" if sib.is_file() else "tree",
                                    sib,
                                    ws_new / slug / sib.name,
                                    tree_size(sib),
                                    f"sibling of {slug} persona",
                                )
                            )
    else:
        plan.blockers.append(f"profiles config missing: {profiles_file}")

    # --- data/ ----------------------------------------------------------
    if data.is_dir():
        for entry in sorted(data.iterdir()):
            name = entry.name
            base_db = next(
                (
                    d
                    for d in DB_NAMES
                    if name == d or name in (d + s for s in DB_SIDECARS)
                ),
                None,
            )
            if base_db:
                if name == base_db:
                    plan.items.append(
                        Item(
                            "db",
                            entry,
                            root / "db" / name,
                            db_size(entry),
                            "sqlite backup API (-wal/-shm folded in)",
                        )
                    )
                continue
            if name in claimed:
                continue
            if name in STAY_IN_REPO:
                plan.stays.append((entry, STAY_IN_REPO[name]))
            elif name in TO_DEVOPS:
                plan.items.append(
                    Item(
                        "tree" if entry.is_dir() else "file",
                        entry,
                        devops / TO_DEVOPS[name],
                        tree_size(entry),
                        "devops workspace",
                    )
                )
            elif name == "model_auth":
                for f in sorted(entry.rglob("*")):
                    if f.is_file():
                        plan.items.append(
                            Item(
                                "secret",
                                f,
                                root / "secrets" / "model_auth" / f.relative_to(entry),
                                f.stat().st_size,
                                "0600 in 0700 dir",
                            )
                        )
            elif name in TO_ARCHIVE:
                plan.items.append(
                    Item(
                        "archive",
                        entry,
                        archive / "Devvybot/devvy_bot/data" / name,
                        tree_size(entry),
                        TO_ARCHIVE[name],
                    )
                )
            else:
                why = next((w for rx, w in ARCHIVE_PATTERNS if rx.search(name)), None)
                if why:
                    plan.items.append(
                        Item(
                            "archive",
                            entry,
                            archive / "Devvybot/devvy_bot/data" / name,
                            tree_size(entry),
                            why,
                        )
                    )
                else:
                    plan.unhandled.append(entry)
    else:
        plan.blockers.append(f"legacy data dir missing: {data}")

    # --- workspaces -----------------------------------------------------
    if old_workspaces.is_dir():
        for agent in sorted(old_workspaces.iterdir()):
            dst = ws_new / agent.name
            plan.items.append(
                Item("tree", agent, dst, tree_size(agent), "workspace (symlinks kept)")
            )
            plan.dir_map[str(agent)] = str(dst)
    else:
        plan.warnings.append(f"no legacy workspaces dir: {old_workspaces}")

    # Two sources landing on the same file is a conflict (e.g. a workspace
    # that already has persona.md and a data/<slug>/agent.md mapped onto it).
    tree_dsts = [i.dst for i in plan.items if i.kind == "tree"]
    for it in plan.items:
        if it.kind in ("file", "secret"):
            for t in tree_dsts:
                rel = _under(it.dst, t)
                if rel is not None and rel.parts:
                    src_tree = next(
                        i.src for i in plan.items if i.dst == t and i.kind == "tree"
                    )
                    if (src_tree / rel).exists():
                        plan.conflicts.append(
                            f"{it.src} and {src_tree / rel} both map to {it.dst}"
                        )
    dsts = [i.dst for i in plan.items]
    for d in sorted({d for d in dsts if dsts.count(d) > 1}):
        plan.conflicts.append(f"several sources map to {d}")

    # --- .env split -----------------------------------------------------
    plan.env_source = _pristine(env_file, archive, repo)
    if plan.env_source.exists():
        env_text = plan.env_source.read_text(encoding="utf-8")
        plan.env_text, plan.secrets_text, plan.secret_keys, plan.config_keys = (
            split_env(env_text, root)
        )
        present = {k for k, _ in parse_env_lines(env_text) if k}
        for var in OVERRIDE_VARS:
            if var in present:
                plan.blockers.append(
                    f".env sets {var}: remove it, the path must derive from {DATA_ROOT_VAR}"
                )
    else:
        plan.blockers.append(f".env missing: {env_file}")

    if plan.profiles_text:
        plan.blockers += check_profiles(plan, profiles_file)

    # --- bot.db path rewrites ---------------------------------------------
    bot_db = bot_db or data / "bot.db"
    if bot_db.exists():
        plan_sql(plan, bot_db)
    return plan


def expected_new_path(plan: Plan, old: Path) -> Path:
    """Where a legacy absolute path lands after the move (identity if it
    is not under the moved trees)."""
    for it in plan.items:
        if it.kind in ("tree", "file") and (
            old == it.src or _under(old, it.src) is not None
        ):
            return it.dst / old.relative_to(it.src)
    return old


def check_profiles(plan: Plan, profiles_file: Path) -> List[str]:
    """Load the old and the rewritten profiles.yaml; every rewritten path
    must resolve (against <root>/workspaces) to where its file is copied, and
    every other field (flags, roles, extra_dirs, mcp) must be unchanged."""
    from src.profiles.registry import load_profile_registry

    errors: List[str] = []
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config"
        cfg.mkdir()
        old_p, new_p = cfg / "old.yaml", cfg / "new.yaml"
        old_p.write_text(
            plan.profiles_source.read_text(encoding="utf-8"), encoding="utf-8"
        )
        new_p.write_text(plan.profiles_text, encoding="utf-8")
        # ${DEVVY_ROOT} must keep meaning the repo, as it does in place.
        env_root = os.environ.get("DEVVY_ROOT")
        os.environ["DEVVY_ROOT"] = str(profiles_file.parent.parent)
        try:
            old_reg = load_profile_registry(old_p, data_root=None)
            new_reg = load_profile_registry(new_p, data_root=plan.root)
        except ValueError as e:
            return [f"profiles.yaml does not load: {e}"]
        finally:
            if env_root is None:
                os.environ.pop("DEVVY_ROOT", None)
            else:
                os.environ["DEVVY_ROOT"] = env_root
    new_by = {p.slug: p for p in new_reg.profiles}
    for op in old_reg.profiles:
        np_ = new_by.get(op.slug)
        if np_ is None:
            errors.append(f"profile {op.slug} lost in rewrite")
            continue
        for attr in ("working_dir", "memory_dir", "persona"):
            want = expected_new_path(plan, getattr(op, attr))
            if getattr(np_, attr) != want:
                errors.append(
                    f"profile {op.slug}.{attr}: resolves to {getattr(np_, attr)}, expected {want}"
                )
        for f in dataclasses.fields(op):
            attr = f.name
            if attr in ("working_dir", "memory_dir", "persona"):
                continue
            if getattr(op, attr) != getattr(np_, attr):
                errors.append(f"profile {op.slug}.{attr} changed in rewrite")
    return errors


def plan_sql(plan: Plan, bot_db: Path) -> None:
    """Count the rows each prefix rewrite would touch (read-only)."""
    conn = sqlite3.connect(ro_uri(bot_db), uri=True)
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table, col in PATH_COLUMNS:
            if table not in tables:
                continue
            for old, new in sorted(plan.dir_map.items()):
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {col} = ? OR {col} LIKE ?",
                    (old, old + "/%"),
                ).fetchone()[0]
                if n:
                    plan.sql.append(SqlUpdate(table, col, old, new, n))
        if "sessions" in tables:
            plan.claude_deactivate = sum(
                conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE is_active AND "
                    "COALESCE(backend, 'claude') = 'claude' AND (project_path = ? OR project_path LIKE ?)",
                    (old, old + "/%"),
                ).fetchone()[0]
                for old in plan.dir_map
            )
            # Rows on dirs this migration does not own (repo, $HOME, stale dirs).
            for table, col in PATH_COLUMNS:
                if table not in tables:
                    continue
                for path, n in conn.execute(
                    f"SELECT {col}, COUNT(*) FROM {table} GROUP BY 1"
                ):
                    if not any(
                        path == o or str(path).startswith(o + "/") for o in plan.dir_map
                    ):
                        plan.unmatched_paths.append((f"{table}.{col}", path, n))
        # New keys must not collide with existing ones (conversation_windows PK).
        if "conversation_windows" in tables:
            for old, new in plan.dir_map.items():
                n = conn.execute(
                    "SELECT COUNT(*) FROM conversation_windows WHERE working_dir = ? OR working_dir LIKE ?",
                    (new, new + "/%"),
                ).fetchone()[0]
                if n:
                    plan.conflicts.append(
                        f"conversation_windows already has rows under {new}"
                    )
    finally:
        conn.close()


# --- rendering -----------------------------------------------------------------


def render(plan: Plan) -> str:
    L: List[str] = []
    w = L.append
    w(f"migrate_data_root ({TICKET}) plan")
    w(f"  repo          {plan.repo}")
    w(f"  data root     {plan.root}")
    w(f"  old workspaces {plan.old_workspaces}")
    w(f"  archive       {plan.archive}")
    if plan.already_applied:
        w(
            f"  STATUS: already applied ({plan.root / MARKER} exists); --apply only re-verifies"
        )
    groups = (
        ("db", "Databases (backup API + integrity_check + row counts)"),
        ("tree", "Directories (copytree, symlinks kept)"),
        ("file", "Files"),
        ("secret", "Secrets (dir 0700, files 0600)"),
        ("archive", "To archive (copy; old tree untouched)"),
    )
    for kind, title in groups:
        rows = [i for i in plan.items if i.kind == kind]
        if not rows:
            continue
        w("")
        w(f"{title}: {human(sum(i.size for i in rows))}")
        for i in rows:
            w(f"  {human(i.size):>9}  {i.src}")
            w(f"             -> {i.dst}" + (f"   [{i.note}]" if i.note else ""))
    w("")
    w(f"Total to copy: {human(plan.total_bytes)}")
    if plan.stays:
        w("")
        w("Stays in the repo:")
        for p, why in plan.stays:
            w(f"  {p}  [{why}]")
    w("")
    w(f".env split (source: {plan.env_source}):")
    w(
        f"  -> {plan.root / 'secrets' / 'bot.env'} (0600): {', '.join(plan.secret_keys) or '(none)'}"
    )
    w(f"  stays in .env (config, EnvironmentFile): {', '.join(plan.config_keys)}")
    w(f"  adds {DATA_ROOT_VAR}={plan.root}")
    w("")
    w(f"profiles.yaml rewrite (source: {plan.profiles_source}):")
    for slug, key, old, new in plan.profile_changes:
        w(f"  {slug:<9} {key:<11} {old}  ->  {new}")
    w(
        "  unchanged: absolute paths outside the old tree (devops working_dir, mcp, extra_dirs); unrestricted/sandbox flags untouched"
    )
    w("")
    w("bot.db path rewrites (on the NEW copy only):")
    for u in plan.sql:
        w(f"  {u.table}.{u.column}: {u.rows} rows  {u.old}[/...] -> {u.new}[/...]")
    w(
        f"  sessions: deactivate {plan.claude_deactivate} active Claude rows on moved dirs "
        "(fresh SDK sessions, seeded from DB; Codex rows stay active)"
    )
    w("  window_bundles: untouched (re-materialise on the new dir)")
    if plan.unmatched_paths:
        w("  left as-is (dirs this migration does not move):")
        for col, path, n in plan.unmatched_paths:
            w(f"    {col}: {path} ({n})")
    for title, rows in (
        ("CONFLICTS (block --apply)", plan.conflicts),
        ("UNHANDLED data/ entries (block --apply)", [str(p) for p in plan.unhandled]),
        ("BLOCKERS", plan.blockers),
        ("Warnings", plan.warnings),
    ):
        if rows:
            w("")
            w(f"{title}:")
            for r in rows:
                w(f"  - {r}")
    w("")
    w("Not done by this script (cutover checklist, manual):")
    w(
        "  - stop claude-bot only at 0 in-flight turns; stop llm-analytics, channel-intel, taskpump.timer"
    )
    w(
        "  - /etc/systemd/system/channel-intel.service: drop Environment=CHANNEL_INTEL_DB_PATH=...; daemon-reload"
    )
    w(
        "  - start services; verify new PID + journal timestamp; /proc/<pid>/environ has no secret names"
    )
    w(
        "  - after verification: chmod -R a-w the old tree (7 days), then move it to the archive"
    )
    w("  - ~/scripts/backup.sh + backup-report.sh still name the old paths (dv-8bad)")
    return "\n".join(L)


# --- apply ---------------------------------------------------------------------


def service_states(
    units_system: Sequence[str], units_user: Sequence[str]
) -> Dict[str, str]:
    states: Dict[str, str] = {}
    for scope, units in (([], units_system), (["--user"], units_user)):
        for u in units:
            try:
                r = subprocess.run(
                    ["systemctl", *scope, "is-active", u],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                states[u] = r.stdout.strip() or "unknown"
            except (OSError, subprocess.TimeoutExpired):
                states[u] = "unknown"
    return states


def table_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def copy_db(src: Path, dst: Path) -> Dict[str, int]:
    """Backup-API copy, then integrity_check + per-table row counts."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    for p in (dst, Path(f"{dst}-wal"), Path(f"{dst}-shm"), Path(f"{dst}.partial")):
        if p.exists():
            p.unlink()
    tmp = Path(f"{dst}.partial")
    s = sqlite3.connect(ro_uri(src), uri=True)
    d = sqlite3.connect(tmp)
    try:
        s.backup(d)
        d.commit()
        src_counts = table_counts(s)
    finally:
        d.close()
        s.close()
    os.replace(tmp, dst)
    d = sqlite3.connect(dst)
    try:
        ok = d.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"integrity_check failed for {dst}: {ok}")
        dst_counts = table_counts(d)
    finally:
        d.close()
    if src_counts != dst_counts:
        diff = {
            k: (src_counts.get(k), dst_counts.get(k))
            for k in set(src_counts) | set(dst_counts)
            if src_counts.get(k) != dst_counts.get(k)
        }
        raise RuntimeError(f"row counts differ for {dst}: {diff}")
    shutil.copymode(src, dst)
    return dst_counts


def rewrite_paths(db: Path, plan: Plan) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute("BEGIN")
        before = table_counts(conn)
        for old, new in plan.dir_map.items():
            conn.execute(
                "UPDATE sessions SET is_active = 0, deactivated_at = CURRENT_TIMESTAMP, "
                "deactivated_reason = ? WHERE is_active AND COALESCE(backend, 'claude') = 'claude' "
                "AND (project_path = ? OR project_path LIKE ?)",
                (f"{TICKET}: data-root move, fresh SDK session", old, old + "/%"),
            )
            for table, col in PATH_COLUMNS:
                conn.execute(
                    f"UPDATE {table} SET {col} = ? || substr({col}, ?) "
                    f"WHERE {col} = ? OR {col} LIKE ?",
                    (new, len(old) + 1, old, old + "/%"),
                )
        if table_counts(conn) != before:
            raise RuntimeError("row counts changed during path rewrite")
        conn.commit()
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"integrity_check failed after rewrite: {ok}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _write_private(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _copy(src: Path, dst: Path, owned: Sequence[Path]) -> None:
    """Copy src to dst, replacing what an earlier unfinished run left there.
    Only ever deletes under ``owned`` (the new root and this run's archive)."""
    if dst.exists() or dst.is_symlink():
        if not any(_under(dst, o) is not None for o in owned):
            raise RuntimeError(
                f"refusing to replace {dst}: outside the migration's own dirs"
            )
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def verify_registry(plan: Plan, profiles_path: Path) -> List[str]:
    """Load the rewritten profiles against the new root; every path must exist."""
    from src.profiles.registry import load_profile_registry, validate_registry_paths

    reg = load_profile_registry(profiles_path, data_root=plan.root)
    validate_registry_paths(reg)
    return [f"{p.slug}: {p.working_dir}" for p in reg.profiles]


def apply(plan: Plan, *, allow_active: Sequence[str], log=print) -> int:
    if plan.already_applied:
        log(
            f"Already applied ({plan.root / MARKER}); nothing copied. Delete the marker to redo "
            "(only while the bot has NOT yet run on the new root)."
        )
        return 0
    problems = (
        plan.blockers + plan.conflicts + [f"unhandled: {p}" for p in plan.unhandled]
    )
    states = service_states(SERVICES_SYSTEM, SERVICES_USER)
    for unit, state in states.items():
        if state in ("active", "activating", "reloading", "deactivating"):
            if unit == "claude-bot.service" or unit not in allow_active:
                problems.append(f"{unit} is {state}")
        elif state == "unknown" and unit == "claude-bot.service":
            problems.append("cannot determine claude-bot.service state")
    free = shutil.disk_usage(
        plan.root.parent if not plan.root.exists() else plan.root
    ).free
    if free < plan.total_bytes * 2:
        problems.append(f"only {human(free)} free for {human(plan.total_bytes)}")
    if problems:
        log("REFUSING --apply:")
        for p in problems:
            log(f"  - {p}")
        return 2

    root = plan.root
    stamp = plan.archive
    # 1. Pristine copies of the config we are about to rewrite, write-once.
    for src in (plan.repo / ".env", plan.repo / "config" / "profiles.yaml"):
        saved = stamp / "pre-migration" / src.relative_to(plan.repo)
        if not saved.exists():
            saved.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(saved.parent, 0o700)
            shutil.copy2(src, saved)
            os.chmod(saved, 0o600)
            log(f"saved {src} -> {saved}")
    # 2. Layout.
    for d, mode in (
        (root, 0o755),
        (root / "db", 0o755),
        (root / "workspaces", 0o755),
        (root / "secrets", 0o700),
        (root / "secrets" / "model_auth", 0o700),
    ):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, mode)
    # 3. Databases.
    for it in (i for i in plan.items if i.kind == "db"):
        counts = copy_db(it.src, it.dst)
        log(
            f"db ok  {it.dst} ({len(counts)} tables, {sum(counts.values())} rows, integrity ok)"
        )
    new_bot = root / "db" / "bot.db"
    if new_bot.exists():
        rewrite_paths(new_bot, plan)
        log(f"rewrote workspace paths in {new_bot}")
    # 4. Files and trees (workspaces first so personas land on top).
    order = {"tree": 0, "file": 1, "archive": 2, "secret": 3}
    for it in sorted(
        (i for i in plan.items if i.kind != "db"), key=lambda i: order[i.kind]
    ):
        _copy(it.src, it.dst, owned=(root, plan.archive))
        if it.kind == "secret":
            os.chmod(it.dst, 0o600)
        log(f"copied {it.src} -> {it.dst}")
    # 5. Config.
    _write_private(root / "secrets" / "bot.env", plan.secrets_text, 0o600)
    log(f"wrote {root / 'secrets' / 'bot.env'} (keys: {', '.join(plan.secret_keys)})")
    profiles = plan.repo / "config" / "profiles.yaml"
    _write_private(profiles, plan.profiles_text, 0o600)
    for line in verify_registry(plan, profiles):
        log(f"profile ok  {line}")
    _write_private(plan.repo / ".env", plan.env_text, 0o600)
    log(f"wrote {plan.repo / '.env'} ({DATA_ROOT_VAR} set, secrets removed)")
    # 6. Archive manifest + completion marker.
    manifest = plan.archive.parent / "MANIFEST.md"
    rows = [
        f"| `{i.src}` | `{i.dst}` | {human(i.size)} | {i.note} (copied; original kept) |"
        for i in plan.items
        if i.kind == "archive"
    ]
    with open(manifest, "a", encoding="utf-8") as f:
        f.write(
            f"\n## {TICKET} data-root migration ({datetime.now(timezone.utc):%Y-%m-%d})\n\n"
            "| original path | archive path | size | reason |\n|---|---|---|---|\n"
            + "\n".join(rows)
            + "\n"
            + f"| `.env`, `config/profiles.yaml` | `{stamp / 'pre-migration'}` | - | pre-migration copies for rollback |\n"
        )
    (root / MARKER).write_text(
        json.dumps(
            {
                "ticket": TICKET,
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "repo": str(plan.repo),
                "old_workspaces": str(plan.old_workspaces),
                "archive": str(plan.archive),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    log(
        "done. Rollback: restore .env + profiles.yaml from the pre-migration copies; the old tree is untouched."
    )
    return 0


# --- CLI -----------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", default=True, help="print the plan (default)"
    )
    mode.add_argument(
        "--apply", action="store_true", help="copy, verify, switch config"
    )
    ap.add_argument("--root", default=os.path.expanduser(DEFAULT_DATA_ROOT))
    ap.add_argument("--old-workspaces", default=str(REPO.parent / "workspaces"))
    ap.add_argument(
        "--archive",
        default=None,
        help="archive dir for this run (default <root>/../archive/<date>-dv-4030)",
    )
    ap.add_argument(
        "--allow-active",
        action="append",
        default=[],
        help="non-bot unit allowed to keep running during --apply (repeatable)",
    )
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser()
    archive = (
        Path(args.archive).expanduser()
        if args.archive
        else (root.parent / "archive" / f"{datetime.now():%Y%m%d}-{TICKET}")
    )
    # Re-runs reuse an existing run dir so pristine config copies are found.
    if not args.archive:
        existing = sorted((root.parent / "archive").glob(f"*-{TICKET}"))
        if existing:
            archive = existing[0]
    plan = build_plan(
        repo=REPO,
        root=root,
        old_workspaces=Path(args.old_workspaces).expanduser(),
        archive=archive,
    )
    print(render(plan))
    if not args.apply:
        print("\n(dry run: nothing written)")
        return 0
    print("")
    return apply(plan, allow_active=args.allow_active)


if __name__ == "__main__":
    sys.exit(main())
