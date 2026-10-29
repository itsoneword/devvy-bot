"""dv-d2a3: the attribution rules one by one (pure functions, no indexer).

The end-to-end scenarios through ``Indexer.scan_once`` live in
``test_attribution.py``; these pin the rules the analysis mapped the odd
groups to (probe, inv, phase-02, scratchpads, server monitor, …)."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from src.llm_analytics.attribution import (
    AgentDirs,
    Attribution,
    CallFacts,
    JobMeta,
    SessionFacts,
    agent_for_path,
    attribute,
    build_context,
    job_attribution,
    load_model_calls,
    match_jobs,
)

HOME = str(Path.home())


def _facts(
    sid: str,
    cwd: str,
    *,
    entrypoint: str = "sdk-py",
    first_ts: str | None = "2026-10-20T10:00:05.000Z",
    in_bot_db: bool = False,
    excerpt: str | None = None,
) -> SessionFacts:
    return SessionFacts(sid, cwd, entrypoint, first_ts, in_bot_db, None, excerpt)


def _attr(
    facts: SessionFacts,
    *,
    calls: dict[str, CallFacts] | None = None,
    jobs: list[JobMeta] | None = None,
    agents: AgentDirs | None = None,
    pump_root: str = "/srv/pump-worktrees",
) -> Attribution:
    ctx = build_context(
        agents=agents or AgentDirs(),
        calls=calls or {},
        jobs=jobs or [],
        sessions=[facts],
        jobs_dir=Path("/srv/jobs"),
        pump_worktrees_dir=Path(pump_root),
        boards=frozenset({"gymbro", "pl"}),
    )
    return attribute(facts, ctx)


def _job(name: str, cwd: str, created: str, sid: str | None = None) -> JobMeta:
    return JobMeta(name, name, cwd, datetime.fromisoformat(created), sid)


class TestJobNames:
    def test_pump_names_old_and_new_and_own_board(self) -> None:
        assert job_attribution("pump-gymbro:gb-1a72-analyze") == Attribution(
            "pump", "gymbro", "gb-1a72/analyze"
        )
        assert job_attribution("pump-gymbro-gb-1a72-architect") == Attribution(
            "pump", "gymbro", "gb-1a72/architect"
        )
        assert job_attribution("pump-dv-d2a3-implement-p1") == Attribution(
            "pump", "devvy", "dv-d2a3/implement-p1"
        )

    def test_other_names_are_jobs(self) -> None:
        assert job_attribution("rel-daily-20261020") == Attribution(
            "job", "rel-daily-20261020"
        )
        assert job_attribution("full-tests").group == "job"


class TestMatchJobs:
    def test_each_job_gets_its_nearest_session_internal_calls_skipped(self) -> None:
        cwd = "/home/x/ws/gymbro"
        internal = _facts("internal", cwd, first_ts="2026-10-20T10:00:01.000Z")
        first = _facts("first", cwd, first_ts="2026-10-20T10:00:04.000Z")
        second = _facts("second", cwd, first_ts="2026-10-20T10:01:04.000Z")
        jobs = [
            _job("a", cwd, "2026-10-20T10:00:00+00:00"),
            _job("b", cwd, "2026-10-20T10:01:00+00:00"),
        ]
        calls = {"internal": CallFacts({"extraction"}, None, None, "extraction")}
        out = match_jobs(jobs, [internal, first, second], calls)
        assert out["first"].name == "a"
        assert out["second"].name == "b"
        assert "internal" not in out

    def test_session_outside_window_or_before_job_is_not_matched(self) -> None:
        cwd = "/home/x/w"
        early = _facts("early", cwd, first_ts="2026-10-20T09:59:59.000Z")
        late = _facts("late", cwd, first_ts="2026-10-20T10:06:00.000Z")
        jobs = [_job("a", cwd, "2026-10-20T10:00:00+00:00")]
        assert match_jobs(jobs, [early, late], {}) == {}

    def test_bot_and_cli_sessions_never_match(self) -> None:
        cwd = "/home/x/w"
        bot = _facts("bot", cwd, in_bot_db=True)
        cli = _facts("cli", cwd, entrypoint="cli")
        jobs = [_job("a", cwd, "2026-10-20T10:00:00+00:00")]
        assert match_jobs(jobs, [bot, cli], {}) == {}


class TestRules:
    def test_cli_scratchpad_probe_is_owner_cli_with_parent_session(self) -> None:
        parent = "6088230c-c41f-4fc7-aa14-0e00d0b31303"
        enc = HOME.replace("/", "-") + "-Devvybot"
        cwd = f"/tmp/claude-1000/{enc}/{parent}/scratchpad/probe"
        assert _attr(_facts("s", cwd)) == Attribution("cli", "Devvybot", parent)

    def test_worktrees_and_home_projects_are_owner_cli(self) -> None:
        wt = f"{HOME}/int_prep/.worktrees/r1-p10"
        assert _attr(_facts("s", wt, entrypoint="sdk-cli")) == Attribution(
            "cli", "int_prep"
        )
        phase = f"{HOME}/gsd-smoke-test/.worktrees/phase-02"
        assert _attr(_facts("s", phase, entrypoint="sdk-cli")).key == "gsd-smoke-test"
        sub = f"{HOME}/Devvybot/devvy_bot/.claude/worktrees/agent-ad1a"
        assert _attr(_facts("s", sub)) == Attribution("cli", "devvy_bot")
        assert _attr(_facts("s", f"{HOME}/openclaw", entrypoint="cli")) == (
            Attribution("cli", "openclaw")
        )

    def test_tmp_probes_and_tests(self) -> None:
        for cwd in ("/tmp/inv/ws", "/tmp/probe586", "/tmp/gate/cwd", "/tmp"):
            assert _attr(_facts("s", cwd)).key == "probes", cwd
        smoke = "/tmp/or-costcap-1/data/workspaces/scratch"
        assert _attr(_facts("s", smoke)) == Attribution("other", "tests", "or-costcap")

    def test_server_monitor_router_and_history(self) -> None:
        monitor = {"s": CallFacts({"monitor"})}
        assert _attr(_facts("s", HOME), calls=monitor) == Attribution(
            "job", "server-monitor"
        )
        old = _facts(
            "s", HOME, entrypoint="sdk-cli", excerpt="Concise server health check + x"
        )
        assert _attr(old) == Attribution("job", "server-monitor")

    def test_history_memory_and_scoring_prompts_are_devvy_internal(self) -> None:
        for excerpt, detail in (
            ("You maintain an agent's long-term memory.\n\n##", "consolidation"),
            ("You reconcile candidate memory facts against", "reconcile"),
            ("You are scoring one turn of a conversation", "answer_scoring"),
        ):
            facts = _facts("s", "/srv/devvy_bot", entrypoint="sdk-cli", excerpt=excerpt)
            assert _attr(facts) == Attribution("other", "devvy-internal", detail)

    def test_background_job_without_job_dir_uses_the_worktree_path(self) -> None:
        calls = {"s": CallFacts({"background_job"})}
        own = _facts("s", "/srv/pump-worktrees/dv-0ecd")
        assert _attr(own, calls=calls) == Attribution(
            "pump", "devvy", "dv-0ecd/unknown"
        )
        board = _facts("s", "/srv/pump-worktrees/pl/dv-1234/src")
        assert _attr(board, calls=calls) == Attribution("pump", "pl", "dv-1234/unknown")
        assert _attr(_facts("s", "/srv/elsewhere"), calls=calls) == Attribution(
            "job", "unknown"
        )

    def test_archive_only_with_a_known_registry(self) -> None:
        facts = _facts("s", "/srv/ws/pm", in_bot_db=True)
        assert _attr(facts).archived is False  # no registry: nothing archived
        agents = AgentDirs(
            by_dir={"/srv/ws/pm": "pm"},
            slugs=frozenset({"pm"}),
            enabled=frozenset(),
            known=True,
        )
        assert _attr(facts, agents=agents) == Attribution("agent", "pm", None, True)


class TestHelpers:
    def test_agent_for_path_users_dir_and_fallback(self) -> None:
        agents = AgentDirs(
            by_dir={"/srv/ws/coach": "coach"},
            slugs=frozenset({"coach"}),
            enabled=frozenset({"coach"}),
            known=True,
        )
        assert agent_for_path("/srv/ws/coach/users/42", agents) == "coach"
        assert agent_for_path("/srv/ws/coach/guests/7", agents) == "coach"
        # Unknown to the registry: the raw cwd label, as before.
        assert agent_for_path("/x/workspaces/chef/users/1", agents) == "chef"
        assert agent_for_path(None, agents) is None

    def test_model_calls_missing_table_or_file_is_empty(self, tmp_path: Path) -> None:
        assert load_model_calls(tmp_path / "absent.db") == {}
        path = tmp_path / "bot.db"
        sqlite3.connect(path).execute("CREATE TABLE sessions (x)").connection.close()
        assert load_model_calls(path) == {}

    def test_model_calls_fold_agent_beats_plain(self, tmp_path: Path) -> None:
        path = tmp_path / "bot.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE model_calls (id INTEGER PRIMARY KEY, session_id TEXT,"
            " purpose TEXT, profile TEXT)"
        )
        conn.executemany(
            "INSERT INTO model_calls (session_id, purpose, profile) VALUES (?,?,?)",
            [("s", "extraction", None), ("s", "chat", "coach"), (None, "chat", "x")],
        )
        conn.commit()
        conn.close()
        calls = load_model_calls(path)
        assert set(calls) == {"s"}
        assert calls["s"].agent_purpose == "chat" and calls["s"].profile == "coach"
        assert _attr(_facts("s", "/srv/x"), calls=calls) == Attribution(
            "agent", "coach", "chat"
        )


def test_now_is_timezone_aware() -> None:
    # Guard for the fixture helper above: created stamps are UTC-aware.
    assert _job("a", "/w", "2026-10-20T10:00:00+00:00").created.tzinfo == UTC
