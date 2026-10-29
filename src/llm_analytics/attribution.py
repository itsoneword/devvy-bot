"""Session attribution by real source (dv-d2a3): one pass in the indexer.

Every indexed session gets ``attr_group`` / ``attr_key`` / ``attr_detail`` /
``attr_archived``. Groups use the dv-87d8 names:

- ``agent``  key = profile slug, detail = router purpose (chat|scheduled)
- ``pump``   key = board (own board ``devvy``), detail = ``<task>/<stage>``
- ``job``    key = bgjob name (server-monitor, rel-daily-…)
- ``cli``    key = project, detail = parent session id for CLI scratchpads
- ``other``  key = ``devvy-internal`` (the bot's own plain calls, detail =
  purpose), ``tests`` (backend_smoke / openrouter acceptance), ``probes``
  (other /tmp dirs) or the cwd basename

``archived`` = an agent slug that is not an enabled profile (disabled or
unknown). It is only set when a profile registry is loaded.

Sources: bot.db ``model_calls`` (purpose/profile per session), the mirrored
``bot_sessions.project_path``, bgjob job dirs (``meta.json`` + the
``session_id`` file agentjob writes; cwd + start time for older jobs), the
pump worktrees layout and ``config/taskpump/boards.json``. ``sessions.profile``
keeps its raw cwd label; this module never rewrites it.

The pass is cheap (one bulk load per source, dict lookups), so the indexer
recomputes every session each cycle and writes only rows that changed. Bump
ATTRIBUTION_VERSION when the rules change; it is recorded in ``meta``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import structlog

from src.utils.paths import REPO_ROOT

logger = structlog.get_logger(__name__)

ATTRIBUTION_VERSION = "1"

GROUPS = ("agent", "pump", "job", "cli", "other")
OWN_BOARD = "devvy"
INTERNAL_KEY = "devvy-internal"
TESTS_KEY = "tests"
PROBES_KEY = "probes"

AGENT_PURPOSES = frozenset({"chat", "scheduled"})
JOB_PURPOSES = frozenset({"background_job", "monitor"})

# A job owns the earliest unclaimed session in its cwd that starts this soon
# after the job dir was created (measured on 667 jobs, analysis §3).
JOB_MATCH_WINDOW_S = 300.0

# scripts/taskpump job names: pump-{task}-{stage}; other boards' task ids are
# board:id, older dirs had ':' -> '-'. No board = this repo's own board.
PUMP_JOB_RE = re.compile(
    r"^pump-(?:(?P<board>[a-z][a-z0-9_]*)[:-])?"
    r"(?P<task>[a-z]{2}-[0-9a-f]{4})-(?P<stage>.+)$"
)
TASK_ID_RE = re.compile(r"^[a-z]{2}-[0-9a-f]{4}$")

# Throwaway data roots of the parity/acceptance scripts: their workspaces
# mirror real profiles (…/workspaces/coach/users/900001) but are test runs.
_TEST_DIR_RE = re.compile(r"^/tmp/(backend-smoke|or-accept|or-costcap)-[^/]*(/|$)")
# Owner CLI scratchpads: /tmp/claude-<uid>/<encoded project>/<session>/scratchpad
_SCRATCHPAD_RE = re.compile(
    r"^/tmp/claude-\d+/(?P<proj>[^/]+)/(?P<sess>[0-9a-f-]{36})/scratchpad(/|$)"
)
_WORKTREE_MARKERS = ("/.claude/worktrees/", "/.worktrees/")

# Pre-router history (before 2026-10-04 there is no model_calls row): the
# first prompt of the bot's own plain calls and of the server monitor.
_HISTORY_PREFIXES: tuple[tuple[str, str, str, str], ...] = (
    (
        "You maintain an agent's long-term memory",
        "other",
        INTERNAL_KEY,
        "consolidation",
    ),
    ("Below are memory facts for one agent", "other", INTERNAL_KEY, "consolidation"),
    ("You reconcile candidate memory facts", "other", INTERNAL_KEY, "reconcile"),
    ("You are scoring ", "other", INTERNAL_KEY, "answer_scoring"),
    ("Concise server health check", "job", "server-monitor", ""),
)


@dataclass(frozen=True)
class Attribution:
    group: str
    key: str
    detail: str | None = None
    archived: bool = False

    def row(self) -> tuple[str, str, str | None, int]:
        return (self.group, self.key, self.detail, 1 if self.archived else 0)


# ----------------------------------------------------------------------
# Profiles
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class AgentDirs:
    """The profile registry as attribution needs it: dir -> slug, enabled set.

    ``known`` is False when no registry is configured or it failed to load;
    then nothing is flagged archived and paths fall back to the cwd label.
    """

    by_dir: dict[str, str] = field(default_factory=dict)
    slugs: frozenset[str] = frozenset()
    enabled: frozenset[str] = frozenset()
    known: bool = False

    def resolve(self, path: str) -> str | None:
        """Slug owning ``path``: a profile root or its users/<id>, guests/<id>."""
        resolved = _resolve(path)
        slug = self.by_dir.get(resolved)
        if slug is not None:
            return slug
        p = PurePosixPath(resolved)
        if p.parent.name in ("users", "guests") and p.name.isdigit():
            return self.by_dir.get(str(p.parent.parent))
        return None

    def archived(self, slug: str) -> bool:
        return self.known and slug not in self.enabled

    def roots(self) -> list[str]:
        return list(self.by_dir)


_AGENT_DIRS_CACHE: dict[str, tuple[float, AgentDirs]] = {}


def load_agent_dirs(path: Path | None) -> AgentDirs:
    """Read profiles.yaml through the bot's own loader (cached by mtime)."""
    if path is None:
        return AgentDirs()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return AgentDirs()
    cached = _AGENT_DIRS_CACHE.get(str(path))
    if cached is not None and cached[0] == mtime:
        return cached[1]
    from src.profiles import load_profile_registry

    try:
        registry = load_profile_registry(path)
    except (ValueError, OSError) as exc:
        logger.warning(
            "attribution_profiles_unreadable", path=str(path), error=str(exc)
        )
        return AgentDirs()
    dirs = AgentDirs(
        by_dir={_resolve(str(p.working_dir)): p.slug for p in registry.profiles},
        slugs=frozenset(p.slug for p in registry.profiles),
        enabled=frozenset(p.slug for p in registry.enabled_profiles),
        known=True,
    )
    _AGENT_DIRS_CACHE[str(path)] = (mtime, dirs)
    return dirs


def agent_dirs_from_env() -> AgentDirs:
    """The live registry (PROFILES_CONFIG_PATH), for read-side callers."""
    raw = os.getenv("PROFILES_CONFIG_PATH")
    return load_agent_dirs(Path(raw).expanduser() if raw else None)


def cwd_label(cwd: str | None) -> str | None:
    """The raw cwd label (``sessions.profile``): the segment after
    ``workspaces/``, else the last path part. Kept as-is for history."""
    if not cwd:
        return None
    # Sessions may record a symlinked cwd (~/chef-workspace -> workspaces/chef);
    # resolve so aliases collapse into one profile.
    with suppress(OSError):
        if os.path.exists(cwd):
            cwd = os.path.realpath(cwd)
    parts = PurePosixPath(cwd).parts
    if "workspaces" in parts:
        idx = parts.index("workspaces")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return parts[-1] if parts else None


def agent_for_path(path: str | None, agents: AgentDirs | None = None) -> str | None:
    """The agent a bot working dir belongs to: the registry first (so the
    repo dir reads ``devops``), the cwd label for paths it does not know."""
    if not path:
        return None
    if agents is None:
        agents = agent_dirs_from_env()
    return agents.resolve(path) or cwd_label(path)


# ----------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------


@dataclass
class CallFacts:
    """model_calls rows of one session, folded."""

    purposes: set[str] = field(default_factory=set)
    agent_purpose: str | None = None
    profile: str | None = None
    plain_purpose: str | None = None


def load_model_calls(bot_db_path: Path) -> dict[str, CallFacts]:
    """session_id -> CallFacts from bot.db (read-only). Missing file or table
    (older bot.db) = no router evidence."""
    if not bot_db_path.exists():
        return {}
    src = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
    try:
        has_table = src.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'model_calls'"
        ).fetchone()
        if not has_table:
            return {}
        rows = src.execute(
            "SELECT session_id, purpose, profile FROM model_calls"
            " WHERE session_id IS NOT NULL ORDER BY id"
        ).fetchall()
    finally:
        src.close()
    out: dict[str, CallFacts] = defaultdict(CallFacts)
    for session_id, purpose, profile in rows:
        facts = out[session_id]
        facts.purposes.add(purpose)
        if purpose in AGENT_PURPOSES:
            facts.agent_purpose = facts.agent_purpose or purpose
            facts.profile = facts.profile or profile
        elif purpose not in JOB_PURPOSES:
            facts.plain_purpose = facts.plain_purpose or purpose
    return dict(out)


@dataclass(frozen=True)
class JobMeta:
    dir_id: str
    name: str
    cwd: str
    created: datetime | None
    session_id: str | None


class JobsCache:
    """bgjob job dirs, ``meta.json`` read once per dir; the ``session_id``
    file is re-checked until it appears (agentjob writes it at turn start)."""

    def __init__(self) -> None:
        self._meta: dict[str, tuple[str, str, datetime | None] | None] = {}
        self._session: dict[str, str] = {}

    def load(self, jobs_dir: Path | None) -> list[JobMeta]:
        if jobs_dir is None or not jobs_dir.is_dir():
            return []
        jobs: list[JobMeta] = []
        for d in sorted(jobs_dir.iterdir()):
            if not d.is_dir():
                continue
            if d.name not in self._meta:
                self._meta[d.name] = _read_meta(d)
            meta = self._meta[d.name]
            if meta is None:
                continue
            if d.name not in self._session:
                with suppress(OSError):
                    sid = (d / "session_id").read_text(encoding="utf-8").strip()
                    if sid:
                        self._session[d.name] = sid
            name, cwd, created = meta
            jobs.append(JobMeta(d.name, name, cwd, created, self._session.get(d.name)))
        return jobs


def _read_meta(d: Path) -> tuple[str, str, datetime | None] | None:
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or not isinstance(meta.get("name"), str):
        return None
    cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else ""
    return (
        meta["name"],
        os.path.normpath(cwd) if cwd else "",
        _parse_ts(meta.get("created")),
    )


def load_boards(path: Path | None) -> frozenset[str]:
    """Other boards' names from boards.json (the own board is implicit)."""
    if path is None:
        return frozenset()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    boards = data.get("boards") if isinstance(data, dict) else None
    return frozenset(
        b["name"]
        for b in boards or []
        if isinstance(b, dict) and isinstance(b.get("name"), str)
    )


# ----------------------------------------------------------------------
# Rules
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class SessionFacts:
    session_id: str
    cwd: str | None
    entrypoint: str | None
    first_ts: str | None
    in_bot_db: bool
    bot_project_path: str | None
    first_excerpt: str | None


@dataclass
class AttributionContext:
    agents: AgentDirs
    calls: dict[str, CallFacts]
    job_of: dict[str, JobMeta]
    pump_root: str | None
    boards: frozenset[str]
    bot_dirs: tuple[str, ...]
    home: str


def match_jobs(
    jobs: Iterable[JobMeta],
    sessions: Iterable[SessionFacts],
    calls: dict[str, CallFacts],
) -> dict[str, JobMeta]:
    """session_id -> its job. The session_id file is exact; otherwise a job
    owns the earliest unclaimed session in its cwd starting within
    JOB_MATCH_WINDOW_S after the job dir was created. Bot sessions, the
    owner's CLI and sessions the router marks as chat/internal never match."""
    jobs = list(jobs)
    out: dict[str, JobMeta] = {}
    for job in jobs:
        if job.session_id:
            out[job.session_id] = job
    by_cwd: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for s in sessions:
        if s.session_id in out or s.in_bot_db or s.entrypoint == "cli" or not s.cwd:
            continue
        facts = calls.get(s.session_id)
        if facts is not None and not (facts.purposes & JOB_PURPOSES):
            continue
        first = _parse_ts(s.first_ts)
        if first is not None:
            by_cwd[os.path.normpath(s.cwd)].append((first, s.session_id))
    for candidates in by_cwd.values():
        candidates.sort()
    claimed = set(out)
    timed = [j for j in jobs if not j.session_id and j.created and j.cwd]
    timed.sort(key=lambda j: (j.created, j.dir_id))
    for job in timed:
        assert job.created is not None
        for first, session_id in by_cwd.get(job.cwd, ()):
            delta = (first - job.created).total_seconds()
            if delta < 0 or session_id in claimed:
                continue
            if delta <= JOB_MATCH_WINDOW_S:
                out[session_id] = job
                claimed.add(session_id)
            break
    return out


def attribute(s: SessionFacts, ctx: AttributionContext) -> Attribution:
    """First matching rule wins (analysis §4). The test-dir rule runs before
    any workspace rule, or smoke runs count as the profile they mirror."""
    cwd = os.path.normpath(s.cwd) if s.cwd else ""
    calls = ctx.calls.get(s.session_id)

    test = _TEST_DIR_RE.match(cwd)
    if test:
        return Attribution("other", TESTS_KEY, test.group(1))
    if calls is not None and calls.agent_purpose:
        slug = calls.profile or agent_for_path(s.bot_project_path or cwd, ctx.agents)
        if slug:
            return _agent(slug, calls.agent_purpose, ctx)
    if s.in_bot_db or s.bot_project_path:
        slug = agent_for_path(s.bot_project_path or cwd, ctx.agents)
        if slug:
            return _agent(slug, None, ctx)
    job = ctx.job_of.get(s.session_id)
    if job is not None:
        return job_attribution(job.name)
    if calls is not None and "monitor" in calls.purposes:
        return Attribution("job", "server-monitor")
    if calls is not None and "background_job" in calls.purposes:
        return _pump_from_path(cwd, ctx) or Attribution("job", "unknown")
    if calls is not None and calls.plain_purpose:
        return Attribution("other", INTERNAL_KEY, calls.plain_purpose)
    if s.first_excerpt:
        for prefix, group, key, detail in _HISTORY_PREFIXES:
            if s.first_excerpt.startswith(prefix):
                return Attribution(group, key, detail or None)
    pump = _pump_from_path(cwd, ctx)
    if pump is not None:
        return pump
    if cwd.endswith("/agents/server-monitor"):
        return Attribution("job", "server-monitor")
    cli = _cli(cwd, s.entrypoint, ctx)
    if cli is not None:
        return cli
    if s.entrypoint == "sdk-cli" and _under_any(cwd, ctx.bot_dirs):
        # A print-mode call in a bot dir with no router row and no known
        # prompt: the bot's own plain calls are the only such caller.
        return Attribution("other", INTERNAL_KEY)
    if cwd == "/tmp" or cwd.startswith("/tmp/"):
        return Attribution("other", PROBES_KEY, PurePosixPath(cwd).name or None)
    label = cwd_label(cwd)
    if label and label in ctx.agents.slugs:
        return _agent(label, None, ctx)
    return Attribution("other", PurePosixPath(cwd).name or "unknown")


def job_attribution(name: str) -> Attribution:
    """A bgjob name: pump-… is Pump > board > task/stage, anything else a job."""
    m = PUMP_JOB_RE.match(name)
    if m:
        board = m.group("board") or OWN_BOARD
        return Attribution("pump", board, f"{m.group('task')}/{m.group('stage')}")
    return Attribution("job", name)


def _agent(slug: str, detail: str | None, ctx: AttributionContext) -> Attribution:
    return Attribution("agent", slug, detail, ctx.agents.archived(slug))


def _pump_from_path(cwd: str, ctx: AttributionContext) -> Attribution | None:
    """<root>/<task> (own board) or <root>/<board>/<task>; stage unknown."""
    if not ctx.pump_root or not _under(cwd, ctx.pump_root):
        return None
    parts = PurePosixPath(cwd).relative_to(ctx.pump_root).parts
    if parts and TASK_ID_RE.match(parts[0]):
        return Attribution("pump", OWN_BOARD, f"{parts[0]}/unknown")
    if (
        len(parts) >= 2
        and TASK_ID_RE.match(parts[1])
        and (not ctx.boards or parts[0] in ctx.boards)
    ):
        return Attribution("pump", parts[0], f"{parts[1]}/unknown")
    return None


def _cli(
    cwd: str, entrypoint: str | None, ctx: AttributionContext
) -> Attribution | None:
    """The owner's interactive/headless CLI: key = project."""
    scratch = _SCRATCHPAD_RE.match(cwd)
    if scratch:
        encoded_home = ctx.home.replace("/", "-")
        proj = scratch.group("proj")
        project = (
            proj[len(encoded_home) + 1 :]
            if proj.startswith(encoded_home + "-")
            else proj
        )
        return Attribution("cli", project or "home", scratch.group("sess"))
    in_worktree = any(marker in cwd + "/" for marker in _WORKTREE_MARKERS)
    under_home = _under(cwd, ctx.home) and not _under_any(cwd, ctx.bot_dirs)
    if entrypoint == "cli" or in_worktree or (cwd and under_home):
        return Attribution("cli", _project(cwd, ctx.home))
    return None


def _project(cwd: str, home: str) -> str:
    for marker in _WORKTREE_MARKERS:
        if marker in cwd + "/":
            cwd = (cwd + "/").split(marker, 1)[0]
            break
    if not cwd or cwd == home:
        return "home"
    return PurePosixPath(cwd).name or "home"


def _under(path: str, root: str) -> bool:
    return bool(root) and (path == root or path.startswith(root.rstrip("/") + "/"))


def _under_any(path: str, roots: Iterable[str]) -> bool:
    return any(_under(path, root) for root in roots)


def _resolve(path: str) -> str:
    with suppress(OSError, RuntimeError):
        return str(Path(path).resolve())
    return os.path.normpath(path)


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# ----------------------------------------------------------------------
# The pass
# ----------------------------------------------------------------------


def build_context(
    *,
    agents: AgentDirs,
    calls: dict[str, CallFacts],
    jobs: list[JobMeta],
    sessions: list[SessionFacts],
    jobs_dir: Path | None,
    pump_worktrees_dir: Path | None,
    boards: frozenset[str],
) -> AttributionContext:
    pump_root = _resolve(str(pump_worktrees_dir)) if pump_worktrees_dir else None
    bot_dirs = [*agents.roots(), _resolve(str(REPO_ROOT))]
    if jobs_dir is not None:
        bot_dirs.append(_resolve(str(jobs_dir)))
    if pump_root:
        bot_dirs.append(pump_root)
    return AttributionContext(
        agents=agents,
        calls=calls,
        job_of=match_jobs(jobs, sessions, calls),
        pump_root=pump_root,
        boards=boards,
        bot_dirs=tuple(bot_dirs),
        home=os.path.normpath(str(Path.home())),
    )
