"""dv-d2a3 phase 1: session attribution by real source, through the indexer.

Every test drives the real entry point (``Indexer.scan_once``) over a tiny
fixture world and reads the result from the index DB, the way the dashboard
does. Contract these tests pin (analysis §4, docs/research/dv-d2a3):

- ``sessions.attr_group`` in {agent, pump, job, cli, other} (dv-87d8 names),
  ``attr_key``, ``attr_detail``, ``attr_archived`` (0/1). Migration 0004.
- ``sessions.profile`` keeps its raw cwd-derived value (nothing deleted).
- pump: key = board, detail = "<task_id>/<stage>".
- agent: key = profile slug; archived when the slug is not an enabled profile.
- other: key "tests" for backend_smoke / openrouter acceptance dirs,
  "devvy-internal" for the bot's own plain calls (owner answer 2026-10-25).
- meta key ``attribution_version``: a value other than the code's
  ATTRIBUTION_VERSION recomputes every session.
- ``AnalyticsConfig`` gains (defaulted) fields ``profiles_path``,
  ``jobs_dir``, ``pump_worktrees_dir`` and ``boards_path``.
- a job dir may hold a ``session_id`` file (written by agentjob/bgjob) that
  links the job to its session exactly; otherwise cwd + time match.
"""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.llm_analytics.config import AnalyticsConfig
from src.llm_analytics.db import connect, init_db
from src.llm_analytics.indexer import Indexer
from src.llm_analytics.pricing import PricingTable
from tests.unit.test_llm_analytics.test_indexer import PRICING

USER = 111


def _sid(n: int) -> str:
    return f"dd2a{n:04d}-0000-0000-0000-{n:012d}"


def _assistant(request_id: str, ts: str, cwd: str, entrypoint: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": ts,
            "uuid": f"uuid-{request_id}",
            "requestId": request_id,
            "cwd": cwd,
            "entrypoint": entrypoint,
            "version": "2.1.220",
            "gitBranch": "main",
            "message": {
                "model": "claude-fable-5",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 60,
                    "cache_creation": {
                        "ephemeral_1h_input_tokens": 60,
                        "ephemeral_5m_input_tokens": 0,
                    },
                },
                "content": [],
            },
        }
    )


class World:
    """A fixture deployment: repo, workspaces, profiles.yaml, boards.json,
    jobs dir, pump worktrees, a bot.db with model_calls, and transcripts."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "Devvybot" / "devvy_bot"
        self.workspaces = root / "Devvybot" / "data" / "workspaces"
        self.jobs = root / "Devvybot" / "jobs"
        self.worktrees = root / "Devvybot" / "pump-worktrees"
        self.projects = root / "projects"
        for d in (self.repo, self.workspaces, self.jobs, self.worktrees):
            d.mkdir(parents=True, exist_ok=True)
        self.projects.mkdir()
        self.profiles_path = root / "profiles.yaml"
        self.boards_path = root / "boards.json"
        self.bot_db = root / "bot.db"
        self._write_profiles()
        self._write_boards()
        self._make_bot_db()
        self._req = 0

    # -- config ---------------------------------------------------------

    def _write_profiles(self) -> None:
        def ws(slug: str) -> Path:
            path = self.workspaces / slug
            path.mkdir(parents=True, exist_ok=True)
            return path

        def entry(
            slug: str, wd: Path, *, enabled: bool = True, default: bool = False
        ) -> str:
            lines = [
                f"  - slug: {slug}",
                f"    name: {slug.title()}",
                f"    working_dir: {wd}",
                f"    memory_dir: {wd}/memory",
                f"    persona: {wd}/persona.md",
            ]
            if default:
                lines.append("    default: true")
            if not enabled:
                lines.append("    enabled: false")
            if slug == "devops":
                lines += ["    sandbox: false", "    shared: true"]
            return "\n".join(lines)

        self.coach_wd = ws("coach")
        self.pm_wd = ws("pm")
        self.channel_wd = ws("channel")
        self.scratch_wd = ws("scratch")
        body = "\n".join(
            [
                "profiles:",
                entry("devops", self.repo, default=True),
                entry("coach", self.coach_wd),
                entry("pm", self.pm_wd, enabled=False),
                entry("channel", self.channel_wd, enabled=False),
                entry("scratch", self.scratch_wd, enabled=False),
            ]
        )
        self.profiles_path.write_text(body + "\n", encoding="utf-8")

    def _write_boards(self) -> None:
        self.boards_path.write_text(
            json.dumps(
                {
                    "boards": [
                        {"name": "gymbro", "repo": str(self.root / "gymbro")},
                        {"name": "pl", "repo": str(self.root / "pl")},
                    ]
                }
            )
        )

    def _make_bot_db(self) -> None:
        conn = sqlite3.connect(self.bot_db)
        conn.executescript("""
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY, user_id INTEGER, project_path TEXT,
                created_at TEXT, last_used TEXT, total_cost REAL,
                total_turns INTEGER, message_count INTEGER, is_active INTEGER);
            CREATE TABLE messages (
                message_id INTEGER PRIMARY KEY, session_id TEXT, user_id INTEGER,
                timestamp TEXT, prompt TEXT, response TEXT, cost REAL,
                duration_ms INTEGER, window_id INTEGER);
            CREATE TABLE conversation_windows (
                user_id INTEGER, working_dir TEXT, window_id INTEGER,
                active_backend TEXT, cumulative_tokens INTEGER,
                last_eval_checkpoint INTEGER, last_checkpoint_message_id INTEGER,
                updated_at TEXT);
            CREATE TABLE tool_calls (
                id INTEGER PRIMARY KEY, session_id TEXT, message_id INTEGER,
                tool TEXT, input TEXT, started_at TEXT,
                status TEXT, error TEXT, parent_tool_use_id TEXT);
            CREATE TABLE memory_facts (
                id INTEGER PRIMARY KEY, agent_id TEXT, tier TEXT, label TEXT,
                content TEXT, status TEXT, kind TEXT, created_at TEXT);
            CREATE TABLE model_calls (
                id INTEGER PRIMARY KEY,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                purpose TEXT NOT NULL, kind TEXT NOT NULL, harness TEXT NOT NULL,
                harness_version TEXT, provider TEXT NOT NULL, model TEXT,
                attended INTEGER NOT NULL DEFAULT 1,
                profile TEXT, user_id INTEGER, session_id TEXT,
                input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
                duration_ms INTEGER, ok INTEGER, error TEXT, cost_source TEXT);
            """)
        conn.commit()
        conn.close()

    def bot_session(self, session_id: str, project_path: Path | str) -> None:
        conn = sqlite3.connect(self.bot_db)
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                USER,
                str(project_path),
                "2026-08-01T09:59:00+00:00",
                None,
                0.1,
                1,
                0,
                0,
            ),
        )
        conn.commit()
        conn.close()

    def model_call(
        self,
        session_id: str,
        purpose: str,
        *,
        profile: str | None = None,
        ts: str | None = None,
    ) -> None:
        conn = sqlite3.connect(self.bot_db)
        conn.execute(
            "INSERT INTO model_calls (ts, purpose, kind, harness, provider, model,"
            " profile, user_id, session_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                ts or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                purpose,
                (
                    "turn"
                    if purpose in ("chat", "scheduled", "background_job")
                    else "plain"
                ),
                "claude",
                "anthropic",
                "claude-fable-5",
                profile,
                USER if purpose == "chat" else None,
                session_id,
            ),
        )
        conn.commit()
        conn.close()

    def job(
        self,
        dir_id: str,
        name: str,
        *,
        cwd: Path,
        created: str,
        profile: str = "devops",
        session_id: str | None = None,
    ) -> Path:
        jobdir = self.jobs / dir_id
        jobdir.mkdir(parents=True)
        (jobdir / "meta.json").write_text(
            json.dumps(
                {
                    "id": dir_id,
                    "name": name,
                    "profile": profile,
                    "cwd": str(cwd),
                    "unit": f"bgjob-{dir_id}",
                    "created": created,
                    "model": "opus",
                    "prompt": "You are the build half of `taskpump`...",
                    "quiet": True,
                }
            )
        )
        if session_id is not None:
            (jobdir / "session_id").write_text(session_id + "\n")
        return jobdir

    # -- transcripts ----------------------------------------------------

    def transcript(
        self,
        session_id: str,
        cwd: Path | str,
        *,
        start: str = "2026-08-01T10:00:00",
        entrypoint: str = "sdk-py",
        calls: int = 1,
    ) -> None:
        folder = self.projects / ("-" + str(cwd).strip("/").replace("/", "-"))
        folder.mkdir(parents=True, exist_ok=True)
        base = datetime.fromisoformat(start)
        lines = []
        for i in range(calls):
            self._req += 1
            ts = base.replace(second=min(59, base.second + i))
            lines.append(
                _assistant(
                    f"req_{self._req}",
                    ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    str(cwd),
                    entrypoint,
                )
            )
        (folder / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n")

    # -- indexer --------------------------------------------------------

    def indexer(self, conn: sqlite3.Connection) -> Indexer:
        pricing_path = self.root / "pricing.json"
        pricing_path.write_text(json.dumps(PRICING))
        config = AnalyticsConfig(
            host="127.0.0.1",
            port=0,
            db_path=self.root / "index.db",
            projects_dir=self.projects,
            bot_db_path=self.bot_db,
            pricing_path=pricing_path,
            scan_interval=30,
            profiles_path=self.profiles_path,
            jobs_dir=self.jobs,
            pump_worktrees_dir=self.worktrees,
            boards_path=self.boards_path,
        )
        return Indexer(conn, config, PricingTable.load(pricing_path))


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    # The live env must not leak a real registry/jobs dir into the fixture.
    monkeypatch.setenv("PROFILES_CONFIG_PATH", str(tmp_path / "profiles.yaml"))
    monkeypatch.setenv("BGJOB_ROOT", str(tmp_path / "Devvybot" / "jobs"))
    monkeypatch.setenv("PUMP_WORKTREES", str(tmp_path / "Devvybot" / "pump-worktrees"))
    return World(tmp_path)


@pytest.fixture()
def db(world: World) -> sqlite3.Connection:
    conn = connect(world.root / "index.db")
    init_db(conn)
    return conn


def _attr(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT profile, attr_group, attr_key, attr_detail, attr_archived"
        " FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    assert row is not None, f"session {session_id} not indexed"
    return dict(row)


# ----------------------------------------------------------------------
# Approved scenarios (binding)
# ----------------------------------------------------------------------


class TestAttribution:
    def test_bot_chat_in_repo_dir_is_agent_devops(
        self, world: World, db: sqlite3.Connection
    ) -> None:
        """Pre-router history: a bot session whose cwd is the repo checkout
        (devops' working_dir) must read agent/devops, never devvy_bot."""
        sid = _sid(1)
        world.bot_session(sid, world.repo)
        world.transcript(sid, world.repo)
        world.indexer(db).scan_once()

        attr = _attr(db, sid)
        assert attr["attr_group"] == "agent"
        assert attr["attr_key"] == "devops"
        assert attr["attr_archived"] == 0
        # The raw cwd label is kept as it was.
        assert attr["profile"] == "devvy_bot"

    def test_pump_job_session_is_pump_board_task_stage(
        self, world: World, db: sqlite3.Connection
    ) -> None:
        """Two pump jobs on the same gymbro worktree (old ':'-free and new
        ':' job names): each session is matched to its job by cwd + time and
        reads Pump > gymbro > gb-1a72 > <stage>."""
        wt = world.worktrees / "gymbro" / "gb-1a72"
        wt.mkdir(parents=True)
        world.job(
            "20261024-074853-pump-gymbro-gb-1a72-testdesign-p1",
            "pump-gymbro-gb-1a72-testdesign-p1",
            cwd=wt,
            created="2026-10-24T07:48:53.100000+00:00",
            profile="gymbro",
        )
        world.job(
            "20261024-075953-pump-gymbro-gb-1a72-implement-p1",
            "pump-gymbro:gb-1a72-implement-p1",
            cwd=wt,
            created="2026-10-24T07:59:53.100000+00:00",
            profile="gymbro",
        )
        early, late = _sid(10), _sid(11)
        world.transcript(early, wt, start="2026-10-24T07:49:03")
        world.transcript(late, wt, start="2026-10-24T08:00:03")
        world.indexer(db).scan_once()

        attr = _attr(db, late)
        assert (attr["attr_group"], attr["attr_key"], attr["attr_detail"]) == (
            "pump",
            "gymbro",
            "gb-1a72/implement-p1",
        )
        attr = _attr(db, early)
        assert (attr["attr_group"], attr["attr_key"], attr["attr_detail"]) == (
            "pump",
            "gymbro",
            "gb-1a72/testdesign-p1",
        )
        # The task id never surfaces as an agent/profile key.
        keys = {r[0] for r in db.execute("SELECT attr_key FROM sessions")}
        assert "gb-1a72" not in keys

    def test_backend_smoke_tmp_sessions_are_other_tests_not_coach(
        self, world: World, db: sqlite3.Connection
    ) -> None:
        """backend_smoke runs under /tmp/backend-smoke-*/data/workspaces/coach:
        they go to other/tests; only the real coach chat counts for coach."""
        coach_cwd = world.coach_wd / "users" / str(USER)
        coach_cwd.mkdir(parents=True)
        real = _sid(20)
        world.bot_session(real, coach_cwd)
        world.transcript(real, coach_cwd)

        smoke = [_sid(21), _sid(22), _sid(23)]
        smoke_cwds = [
            "/tmp/backend-smoke-k3j2/data/workspaces/coach/users/900001",
            "/tmp/backend-smoke-zz91/data/workspaces/coach/users/900001",
            "/tmp/or-accept-abc/data/workspaces/scratch",
        ]
        for sid, cwd in zip(smoke, smoke_cwds):
            world.transcript(sid, cwd)
        world.indexer(db).scan_once()

        assert _attr(db, real)["attr_group"] == "agent"
        assert _attr(db, real)["attr_key"] == "coach"
        for sid in smoke:
            attr = _attr(db, sid)
            assert (attr["attr_group"], attr["attr_key"]) == ("other", "tests"), sid
        coach_sessions = db.execute(
            "SELECT COUNT(*) FROM sessions"
            " WHERE attr_group = 'agent' AND attr_key = 'coach'"
        ).fetchone()[0]
        assert coach_sessions == 1
        # Raw label untouched: the smoke run still says coach in `profile`.
        assert _attr(db, smoke[0])["profile"] == "coach"

    def test_dead_and_unknown_profiles_archived_raw_profile_kept(
        self, world: World, db: sqlite3.Connection
    ) -> None:
        """Disabled profiles (pm, channel, scratch) and a slug the registry
        does not know are agent rows flagged archived; a live one is not.
        sessions.profile keeps its raw value."""
        pm, channel, scratch, unknown, coach = (
            _sid(30),
            _sid(31),
            _sid(32),
            _sid(33),
            _sid(34),
        )
        world.bot_session(pm, world.pm_wd)
        world.transcript(pm, world.pm_wd)
        ch_cwd = world.channel_wd / "users" / str(USER)
        ch_cwd.mkdir(parents=True)
        world.bot_session(channel, ch_cwd)
        world.transcript(channel, ch_cwd)
        world.bot_session(scratch, world.scratch_wd)
        world.transcript(scratch, world.scratch_wd)
        # A chat turn recorded by the router under a profile that no longer
        # exists in profiles.yaml.
        gone_wd = world.root / "old" / "ws"
        gone_wd.mkdir(parents=True)
        world.model_call(unknown, "chat", profile="ws", ts="2026-10-05 10:00:00")
        world.transcript(unknown, gone_wd, start="2026-10-05T10:00:00")
        coach_cwd = world.coach_wd / "users" / str(USER)
        coach_cwd.mkdir(parents=True)
        world.bot_session(coach, coach_cwd)
        world.transcript(coach, coach_cwd)
        world.indexer(db).scan_once()

        for sid, slug in ((pm, "pm"), (channel, "channel"), (scratch, "scratch")):
            attr = _attr(db, sid)
            assert (attr["attr_group"], attr["attr_key"]) == ("agent", slug)
            assert attr["attr_archived"] == 1, slug
            assert attr["profile"] == slug  # raw column kept
        attr = _attr(db, unknown)
        assert (attr["attr_group"], attr["attr_key"]) == ("agent", "ws")
        assert attr["attr_archived"] == 1
        assert attr["profile"] == "ws"
        assert _attr(db, coach)["attr_archived"] == 0

    def test_version_bump_recomputes_all_old_sessions_nothing_deleted(
        self, world: World, db: sqlite3.Connection
    ) -> None:
        """Old sessions carrying a stale attribution are all recomputed once
        the stored attribution_version differs from the code's; row counts and
        the raw profile column are unchanged."""
        bot = _sid(40)
        world.bot_session(bot, world.repo)
        world.transcript(bot, world.repo, calls=2)
        smoke = _sid(41)
        world.transcript(
            smoke, "/tmp/backend-smoke-q1/data/workspaces/coach/users/900001"
        )
        cli = _sid(42)
        world.transcript(cli, world.root / "openclaw", entrypoint="cli")
        indexer = world.indexer(db)
        indexer.scan_once()

        def snapshot() -> tuple[int, int, int, list[tuple[str, str]]]:
            return (
                db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
                [
                    (r[0], r[1])
                    for r in db.execute(
                        "SELECT session_id, profile FROM sessions ORDER BY 1"
                    )
                ],
            )

        before = snapshot()
        expected = {
            sid: (_attr(db, sid)["attr_group"], _attr(db, sid)["attr_key"])
            for sid in (bot, smoke, cli)
        }
        assert expected[bot] == ("agent", "devops")
        assert expected[smoke] == ("other", "tests")
        assert expected[cli][0] == "cli"

        # Simulate rows written by an older attribution version (and by the
        # pre-attribution indexer: the cwd label everywhere).
        db.execute(
            "UPDATE sessions SET attr_group = 'agent', attr_key = profile,"
            " attr_detail = NULL, attr_archived = 0"
        )
        db.execute(
            "INSERT INTO meta (key, value) VALUES ('attribution_version', 'stale')"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        db.commit()

        indexer.scan_once()

        for sid, want in expected.items():
            attr = _attr(db, sid)
            assert (attr["attr_group"], attr["attr_key"]) == want, sid
        assert snapshot() == before  # nothing deleted, raw profile kept
        stored = db.execute(
            "SELECT value FROM meta WHERE key = 'attribution_version'"
        ).fetchone()[0]
        assert stored != "stale"

    # ------------------------------------------------------------------
    # Added scenarios
    # ------------------------------------------------------------------

    def test_session_id_file_in_job_dir_beats_timing(
        self, world: World, db: sqlite3.Connection
    ) -> None:
        """Going forward agentjob writes <jobdir>/session_id: that exact link
        wins over the cwd+time guess, even when another session is closer."""
        wt = world.worktrees / "dv-0ecd"
        wt.mkdir(parents=True)
        linked, closer = _sid(50), _sid(51)
        world.job(
            "20261020-100000-pump-dv-0ecd-review-p2",
            "pump-dv-0ecd-review-p2",
            cwd=wt,
            created="2026-10-20T10:00:00+00:00",
            session_id=linked,
        )
        world.transcript(closer, wt, start="2026-10-20T10:00:02")
        world.transcript(linked, wt, start="2026-10-20T10:03:00")
        world.indexer(db).scan_once()

        attr = _attr(db, linked)
        assert (attr["attr_group"], attr["attr_key"], attr["attr_detail"]) == (
            "pump",
            "devvy",
            "dv-0ecd/review-p2",
        )
        assert _attr(db, closer)["attr_detail"] != "dv-0ecd/review-p2"

    def test_bot_internal_plain_calls_are_other_devvy_internal(
        self, world: World, db: sqlite3.Connection
    ) -> None:
        """Owner answer 2026-10-25: memory/reconcile/scoring calls go inside
        `other` as devvy-internal, even though their cwd is the repo dir."""
        sid = _sid(60)
        world.model_call(sid, "extraction", ts="2026-10-06 12:00:00")
        world.transcript(
            sid, world.repo, start="2026-10-06T12:00:00", entrypoint="sdk-cli"
        )
        world.indexer(db).scan_once()

        attr = _attr(db, sid)
        assert (attr["attr_group"], attr["attr_key"]) == ("other", "devvy-internal")
        assert attr["attr_detail"] == "extraction"

    def test_model_call_arriving_later_reattributes_session(
        self, world: World, db: sqlite3.Connection
    ) -> None:
        """A session indexed before its model_calls row exists is
        re-attributed on a later cycle once the row appears."""
        sid = _sid(70)
        world.transcript(
            sid, world.repo, start="2026-10-06T12:00:00", entrypoint="sdk-cli"
        )
        indexer = world.indexer(db)
        indexer.scan_once()
        assert _attr(db, sid)["attr_group"] is not None
        assert _attr(db, sid)["attr_detail"] != "consolidation"

        world.model_call(sid, "consolidation")
        indexer.scan_once()

        attr = _attr(db, sid)
        assert (attr["attr_group"], attr["attr_key"]) == ("other", "devvy-internal")
        assert attr["attr_detail"] == "consolidation"
