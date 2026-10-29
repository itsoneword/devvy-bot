"""Read-API tests against a small fixture DB built via the real indexer.

The fixture corpus covers: a bot chain with one observed window rotation
(S1 -> S2), regular + workflow subagent layouts, an sdk-py orphan transcript,
cli / sdk-cli sessions (expected-non-bot), and bot-only sessions with and
without retention-age transcripts.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.llm_analytics.config import AnalyticsConfig
from src.llm_analytics.db import connect, init_db
from src.llm_analytics.indexer import Indexer
from src.llm_analytics.pricing import PricingTable
from src.llm_analytics.server import build_app
from tests.unit.test_llm_analytics.test_indexer import PRICING

S1 = "aaaa0001-0000-0000-0000-000000000001"
S2 = "aaaa0002-0000-0000-0000-000000000002"
ORPHAN = "aaaa0003-0000-0000-0000-000000000003"
CLI = "aaaa0004-0000-0000-0000-000000000004"
SDK_CLI = "aaaa0005-0000-0000-0000-000000000005"
GONE = "aaaa0006-0000-0000-0000-000000000006"  # bot-only, recent
OLD = "aaaa0007-0000-0000-0000-000000000007"  # bot-only, pre-retention

USER = 111
PROJ = "/home/u/proj"

# Per-call usage: input 3, output 10, cache_read 50, cache_creation 60 (1h).
CALL_COST = (3 * 5.0 + 10 * 25.0 + 50 * 0.50 + 60 * 10.0) / 1e6


def _entry(
    request_id: str,
    ts: str,
    *,
    entrypoint: str = "sdk-py",
    cwd: str = PROJ,
    content: list[dict[str, Any]] | None = None,
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": ts,
            "uuid": f"uuid-{request_id}",
            "parentUuid": f"parent-{request_id}",
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
                "content": content or [],
            },
        }
    )


def _user(ts: str, text: str, *, entrypoint: str = "sdk-py", cwd: str = PROJ) -> str:
    return json.dumps(
        {
            "type": "user",
            "timestamp": ts,
            "uuid": f"user-{ts}",
            "cwd": cwd,
            "entrypoint": entrypoint,
            "version": "2.1.220",
            "message": {"role": "user", "content": text},
        }
    )


def _make_bot_db(path: Path) -> None:
    conn = sqlite3.connect(path)
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
        """)
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (S1, USER, PROJ, "2026-08-01T09:59:00+00:00", None, 0.5, 2, 2, 0),
            (S2, USER, PROJ, "2026-08-01T10:59:00+00:00", None, 0.001, 1, 1, 1),
            # message_count 3 with 0 message rows -> turn_count_mismatch.
            (
                GONE,
                USER,
                "/home/u/other",
                "2026-08-02T09:00:00+00:00",
                None,
                0.2,
                3,
                3,
                0,
            ),
            (
                OLD,
                USER,
                "/home/u/other",
                "2026-07-01T09:00:00+00:00",
                None,
                0.1,
                1,
                1,
                0,
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
        [
            # msg 1: log claim backed by a log_* call -> NOT claimed_not_logged
            (1, S1, USER, "2026-08-01T09:59:30+00:00", "hi", "записал", 0.2, 900, 1),
            # msg 2: log claim with no log_* call -> claimed_not_logged
            (2, S1, USER, "2026-08-01T10:00:30+00:00", "more", "Записал!", 0.3, 900, 1),
            # msg 3: complaint in the user prompt
            (
                3,
                S2,
                USER,
                "2026-08-01T10:59:30+00:00",
                "зачем ты это удалил",
                "r",
                0.001,
                900,
                2,
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO tool_calls (id, session_id, message_id, tool, input,"
        " started_at, status, error) VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                1,
                S1,
                1,
                "mcp__agent__log_food",
                "{}",
                "2026-08-01T09:59:29+00:00",
                "ok",
                None,
            ),
            (2, S1, 1, "Bash", "{}", "2026-08-01T09:59:29+00:00", "ok", None),
        ],
    )
    conn.executemany(
        "INSERT INTO memory_facts VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                1,
                f"proj#u{USER}",
                "pinned",
                None,
                "no emoji",
                "active",
                "user_fact",
                "2026-08-01 10:05:00",
            ),
            (
                2,
                "proj",
                "general",
                None,
                "likes tea",
                "active",
                "user_fact",
                "2026-08-01 10:06:00",
            ),
        ],
    )
    conn.execute(
        "INSERT INTO conversation_windows VALUES (?,?,?,?,?,?,?,?)",
        (USER, PROJ, 2, "claude", 26, 50000, 3, "2026-08-01T11:00:00+00:00"),
    )
    conn.commit()
    conn.close()


def _build_fixture(tmp_path: Path) -> AnalyticsConfig:
    proj_dir = tmp_path / "projects" / "-home-u-proj"
    proj_dir.mkdir(parents=True)
    (proj_dir / f"{S1}.jsonl").write_text(
        "\n".join(
            [
                _user("2026-08-01T09:59:30.000Z", "hi"),
                _entry("req_s1_1", "2026-08-01T10:00:00.000Z"),
                _entry(
                    "req_s1_2",
                    "2026-08-01T10:01:00.000Z",
                    content=[
                        {
                            "type": "tool_use",
                            "id": "toolu_01",
                            "name": "Bash",
                            "input": {"command": "ls", "description": "list"},
                        }
                    ],
                ),
            ]
        )
        + "\n"
    )
    sub_dir = proj_dir / S1 / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-abc.jsonl").write_text(
        _entry("req_sub1", "2026-08-01T10:00:30.000Z") + "\n"
    )
    wf_dir = sub_dir / "workflows" / "wf_1"
    wf_dir.mkdir(parents=True)
    (wf_dir / "agent-def.jsonl").write_text(
        _entry("req_wf1", "2026-08-01T10:00:40.000Z") + "\n"
    )
    (wf_dir / "journal.jsonl").write_text('{"type": "journal"}\n')
    (proj_dir / f"{S2}.jsonl").write_text(
        "\n".join(
            [
                _user(
                    "2026-08-01T10:59:58.000Z", "<session_notes>seed</session_notes>"
                ),
                _entry("req_s2_1", "2026-08-01T11:00:00.000Z"),
            ]
        )
        + "\n"
    )
    (proj_dir / f"{ORPHAN}.jsonl").write_text(
        _entry("req_orph", "2026-08-01T12:00:00.000Z") + "\n"
    )
    (proj_dir / f"{CLI}.jsonl").write_text(
        "\n".join(
            [
                _user("2026-08-01T13:00:00.000Z", "cli prompt", entrypoint="cli"),
                _entry("req_cli", "2026-08-01T13:00:05.000Z", entrypoint="cli"),
            ]
        )
        + "\n"
    )
    (proj_dir / f"{SDK_CLI}.jsonl").write_text(
        _entry("req_sdkcli", "2026-08-01T14:00:00.000Z", entrypoint="sdk-cli") + "\n"
    )
    _make_bot_db(tmp_path / "bot.db")
    pricing_path = tmp_path / "pricing.json"
    pricing_path.write_text(json.dumps(PRICING))
    config = AnalyticsConfig(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "index.db",
        projects_dir=tmp_path / "projects",
        bot_db_path=tmp_path / "bot.db",
        pricing_path=pricing_path,
        scan_interval=3600,  # background loop scans once at startup only
    )
    # Pre-build the index synchronously so requests never race the
    # background startup scan.
    conn = connect(config.db_path)
    init_db(conn)
    Indexer(conn, config, PricingTable.load(pricing_path)).scan_once()
    conn.close()
    return config


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Any]:
    config = _build_fixture(tmp_path)
    return {"config": config, "tmp_path": tmp_path}


@pytest.fixture()
def client(env: dict[str, Any]) -> Any:
    with TestClient(build_app(env["config"])) as test_client:
        yield test_client


class TestBasics:
    def test_static_index(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "LLM Analytics" in response.text

    def test_user_id_local_selects_null_user_sessions(self, client: TestClient) -> None:
        body = client.get("/api/sessions", params={"user_id": "local"}).json()
        assert body["total"] > 0
        assert all(item["user_id"] is None for item in body["sessions"])

    def test_user_id_invalid_is_400(self, client: TestClient) -> None:
        assert client.get("/api/overview", params={"user_id": "abc"}).status_code == 400

    def test_filters_scoped_to_range(self, client: TestClient) -> None:
        all_time = client.get("/api/filters").json()
        assert "local" in all_time["users"]
        scoped = client.get(
            "/api/filters", params={"from": "2099-01-01T00:00:00Z"}
        ).json()
        assert scoped["profiles"] == []
        assert scoped["users"] == []

    def test_health(self, client: TestClient) -> None:
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["files"] == 7  # 5 session + 2 subagent (journal ignored)
        assert body["sessions"] == 5
        assert body["api_calls"] == 8
        assert body["last_scan_at"] is not None

    def test_filters(self, client: TestClient) -> None:
        body = client.get("/api/filters").json()
        assert body["users"] == [USER, "local"]
        assert body["entrypoints"] == ["cli", "sdk-cli", "sdk-py"]
        assert body["models"] == ["claude-fable-5"]
        assert "proj" in body["profiles"]
        assert body["date_range"]["from"].startswith("2026-08-01T10:00:00")


class TestOverview:
    def test_totals(self, client: TestClient) -> None:
        body = client.get("/api/overview").json()
        assert body["totals"]["api_calls"] == 8
        assert body["totals"]["output_tokens"] == 80
        assert body["totals"]["sessions"] == 5
        assert body["totals"]["tool_calls"] == 1
        assert body["totals"]["est_cost_usd"] == pytest.approx(8 * CALL_COST)
        assert [d["date"] for d in body["daily"]] == ["2026-08-01"]
        assert body["by_model"][0]["model"] == "claude-fable-5"

    def test_user_filter(self, client: TestClient) -> None:
        body = client.get("/api/overview", params={"user_id": USER}).json()
        assert body["totals"]["api_calls"] == 5  # S1 (4 incl subagents) + S2

    def test_date_filter(self, client: TestClient) -> None:
        body = client.get(
            "/api/overview", params={"from": "2026-08-02", "to": "2026-08-03"}
        ).json()
        assert body["totals"]["api_calls"] == 0


class TestQuality:
    def test_daily_counts(self, client: TestClient) -> None:
        body = client.get("/api/quality").json()
        assert [d["date"] for d in body["daily"]] == ["2026-08-01"]
        day = body["daily"][0]
        assert day["pinned_facts"] == 1  # general-tier fact excluded
        assert day["log_calls"] == 1  # Bash call excluded
        assert day["claimed_not_logged"] == 1  # msg 2; msg 1 backed by log_food
        assert day["complaint_hits"] == 1  # «зачем ты» in msg 3
        assert day["user_messages"] == 3
        # dv-9f41 counters are exposed (fixture has no Web*/programme turns).
        assert day["deep_research_turns"] == 0
        assert day["program_turns_unchecked"] == 0

    def test_by_agent_split(self, client: TestClient) -> None:
        body = client.get("/api/quality").json()
        rows = {(r["profile"], r["user_id"]) for r in body["by_agent"]}
        # bot messages resolve to (proj, 111) via sessions.project_path;
        # the pinned fact's agent_id 'proj#u111' parses to the same scope.
        assert ("proj", USER) in rows

    def test_filters(self, client: TestClient) -> None:
        body = client.get("/api/quality", params={"profile": "other"}).json()
        assert body["totals"]["complaint_hits"] == 0
        assert body["totals"]["user_messages"] == 0
        body = client.get("/api/quality", params={"user_id": USER}).json()
        assert body["totals"]["user_messages"] == 3
        body = client.get(
            "/api/quality", params={"from": "2026-08-02", "to": "2026-08-03"}
        ).json()
        assert body["daily"] == []


class TestSessions:
    def test_pagination(self, client: TestClient) -> None:
        body = client.get("/api/sessions", params={"limit": 2}).json()
        assert body["total"] == 5
        assert len(body["sessions"]) == 2
        assert body["limit"] == 2 and body["offset"] == 0
        page2 = client.get("/api/sessions", params={"limit": 2, "offset": 4}).json()
        assert len(page2["sessions"]) == 1

    def test_entrypoint_filter(self, client: TestClient) -> None:
        body = client.get("/api/sessions", params={"entrypoint": "sdk-py"}).json()
        assert body["total"] == 3  # S1, S2, orphan

    def test_bad_sort_rejected(self, client: TestClient) -> None:
        response = client.get("/api/sessions", params={"sort": "evil; DROP"})
        assert response.status_code == 400

    def test_detail(self, client: TestClient) -> None:
        body = client.get(f"/api/sessions/{S1}").json()
        assert body["session"]["api_call_count"] == 4
        assert body["session"]["user_id"] == USER
        assert body["session"]["in_bot_db"] == 1
        assert body["missing_transcript"] is False
        assert len(body["subagents"]) == 2
        assert len(body["turns"]) == 2  # bot turns mirrored from bot.db
        assert body["session"]["seeded_start"] == 0
        seeded = client.get(f"/api/sessions/{S2}").json()
        assert seeded["session"]["seeded_start"] == 1

    def test_detail_404(self, client: TestClient) -> None:
        assert client.get("/api/sessions/nope").status_code == 404


class TestTimeline:
    def test_main_thread_default(self, client: TestClient) -> None:
        body = client.get(f"/api/sessions/{S1}/timeline").json()
        assert [c["request_id"] for c in body["calls"]] == ["req_s1_1", "req_s1_2"]
        assert [c["cum_output_tokens"] for c in body["calls"]] == [10, 20]
        assert body["calls"][0]["context_provided"] == 3 + 50 + 60
        assert len(body["tools"]) == 1
        assert body["tools"][0]["name"] == "Bash"
        assert body["truncated"] is False

    def test_all_lanes(self, client: TestClient) -> None:
        body = client.get(
            f"/api/sessions/{S1}/timeline", params={"agent_id": "*"}
        ).json()
        assert len(body["calls"]) == 4
        # Cumulatives are per lane, not global.
        by_agent = {c["request_id"]: c for c in body["calls"]}
        assert by_agent["req_sub1"]["cum_output_tokens"] == 10

    def test_single_agent(self, client: TestClient) -> None:
        body = client.get(
            f"/api/sessions/{S1}/timeline", params={"agent_id": "def"}
        ).json()
        assert [c["request_id"] for c in body["calls"]] == ["req_wf1"]


class TestCalls:
    def test_call_detail(self, client: TestClient) -> None:
        body = client.get("/api/calls/req_s1_1").json()
        assert body["session_id"] == S1
        assert body["context_provided"] == 113
        assert body["est_cost_usd"] == pytest.approx(CALL_COST)
        assert client.get("/api/calls/nope").status_code == 404

    def test_raw_is_metadata_only(self, client: TestClient) -> None:
        body = client.get("/api/calls/req_s1_2/raw").json()
        assert body["requestId"] == "req_s1_2"
        assert body["type"] == "assistant"
        assert body["entrypoint"] == "sdk-py"
        assert set(body["message"].keys()) == {"model", "usage", "tool_uses"}
        assert body["message"]["tool_uses"] == [{"id": "toolu_01", "name": "Bash"}]
        # No content text, no tool inputs anywhere in the response.
        assert "content" not in json.dumps(body)
        assert "ls" not in json.dumps(body["message"])

    def test_raw_include_content_501(self, client: TestClient) -> None:
        response = client.get(
            "/api/calls/req_s1_1/raw", params={"include_content": "true"}
        )
        assert response.status_code == 501

    def test_raw_404_when_file_rotated(
        self, client: TestClient, env: dict[str, Any]
    ) -> None:
        path = env["tmp_path"] / "projects" / "-home-u-proj" / f"{ORPHAN}.jsonl"
        path.unlink()
        assert client.get("/api/calls/req_orph/raw").status_code == 404


class TestReconciliation:
    def test_summary_and_flags(self, client: TestClient) -> None:
        body = client.get("/api/reconciliation").json()
        summary = body["summary"]
        assert summary["bot_sessions"] == 4
        assert summary["with_transcript"] == 2
        assert summary["missing_transcript"] == 2
        assert summary["orphan_transcripts"] == 1
        rows = {row["session_id"]: row for row in body["rows"]}
        assert "cost_mismatch" in rows[S1]["flags"]  # bot $0.5 vs est ~$0.004
        assert rows[S2]["flags"] == []
        assert "missing_transcript" in rows[GONE]["flags"]
        assert "turn_count_mismatch" in rows[GONE]["flags"]
        assert rows[OLD]["flags"] == ["missing_transcript_retention"]
        assert rows[S1]["disk"]["present"] is True
        assert rows[GONE]["disk"]["present"] is False

    def test_orphans_exclude_cli_and_sdk_cli(self, client: TestClient) -> None:
        body = client.get("/api/reconciliation").json()
        assert [o["session_id"] for o in body["orphans"]] == [ORPHAN]

    def test_profile_filter(self, client: TestClient) -> None:
        body = client.get("/api/reconciliation", params={"profile": "proj"}).json()
        assert body["summary"]["bot_sessions"] == 2  # S1, S2 only


class TestChains:
    def test_chain_rotation_observed(self, client: TestClient) -> None:
        body = client.get("/api/chains").json()
        chain = next(
            c
            for c in body["chains"]
            if c["user_id"] == USER and c["working_dir"] == PROJ
        )
        assert chain["rotations_observed"] == 1
        assert [w["window_id"] for w in chain["windows"]] == [1, 2]
        assert chain["windows"][0]["message_count"] == 2
        assert chain["current_window"]["cumulative_tokens"] == 26
        sessions = chain["sessions"]
        assert sessions[0]["session_id"] == S1
        assert sessions[0]["rotated_into"] == S2
        assert sessions[1]["rotated_into"] is None
        assert sessions[1]["seeded_start"] is True
        assert sessions[0]["bot_formula_tokens"] == 4 * 13

    def test_chain_detail_counter_replay(self, client: TestClient) -> None:
        body = client.get(
            "/api/chains/detail",
            params={"user_id": USER, "working_dir": PROJ},
        ).json()
        series = body["series"]
        # Main-thread calls only: 2 in S1, 1 in S2 (reset at the rotation).
        assert [p["bot_counter_cum"] for p in series] == [13, 26, 13]
        assert [p["window_id"] for p in series] == [1, 1, 2]
        assert [p["first_of_session"] for p in series] == [True, False, True]
        assert series[0]["session_id"] == S1
        assert series[-1]["session_id"] == S2
        # Threshold annotations track the live Settings defaults (see
        # settings.py window_* fields).
        assert body["annotations"] == {
            "eval_threshold": 120000,
            "refresh_threshold": 150000,
            "db_current_counter": 26,
        }

    def test_chain_detail_since_filter(self, client: TestClient) -> None:
        body = client.get(
            "/api/chains/detail",
            params={
                "user_id": USER,
                "working_dir": PROJ,
                "since": "2026-08-01T10:30:00.000Z",
            },
        ).json()
        assert [p["session_id"] for p in body["series"]] == [S2]

    def test_chains_range_limits_windows_and_sessions(self, client: TestClient) -> None:
        def chain_for(**params: str) -> dict:
            body = client.get("/api/chains", params=params).json()
            return next(
                c
                for c in body["chains"]
                if c["user_id"] == USER and c["working_dir"] == PROJ
            )

        full = chain_for()
        late = chain_for(since="2026-08-01T10:30:00.000Z")
        assert [w["window_id"] for w in late["windows"]] == [2]
        assert [s["session_id"] for s in late["sessions"]] == [S2]
        assert len(full["windows"]) == 2
        empty = chain_for(until="2000-01-01")
        assert empty["windows"] == [] and empty["sessions"] == []

    def test_chain_detail_until_filter(self, client: TestClient) -> None:
        body = client.get(
            "/api/chains/detail",
            params={"user_id": USER, "working_dir": PROJ, "until": "2000-01-01"},
        ).json()
        assert body["series"] == []

    def test_chain_detail_404(self, client: TestClient) -> None:
        response = client.get(
            "/api/chains/detail",
            params={"user_id": 999, "working_dir": "/nope"},
        )
        assert response.status_code == 404


class TestSourceAttribution:
    """dv-d2a3: the API reads the attributed source, not the cwd label."""

    def test_by_source_sums_to_totals(self, client: TestClient) -> None:
        body = client.get("/api/overview").json()
        rows = body["by_source"]
        assert sum(r["api_calls"] for r in rows) == body["totals"]["api_calls"]
        assert sum(r["est_cost_usd"] for r in rows) == pytest.approx(
            body["totals"]["est_cost_usd"]
        )
        assert {(r["group"], r["key"]) for r in rows} >= {("agent", "proj")}

    def test_profile_link_equals_agent_source(self, client: TestClient) -> None:
        old = client.get("/api/overview", params={"profile": "proj"}).json()
        new = client.get("/api/overview", params={"source": "agent/proj"}).json()
        assert old["totals"] == new["totals"]
        assert old["totals"]["api_calls"] == 5  # S1 (incl. subagents) + S2

    def test_sessions_source_filter(self, client: TestClient) -> None:
        body = client.get("/api/sessions", params={"source": "cli"}).json()
        assert [s["session_id"] for s in body["sessions"]] == [CLI]
        assert body["sessions"][0]["attr_group"] == "cli"
        body = client.get("/api/sessions", params={"source": "agent/proj"}).json()
        assert {s["session_id"] for s in body["sessions"]} == {S1, S2}

    def test_filters_profiles_are_live_agents_sources_listed(
        self, client: TestClient
    ) -> None:
        body = client.get("/api/filters").json()
        assert body["profiles"] == ["proj"]
        groups = [s["group"] for s in body["sources"]]
        assert groups == sorted(
            groups, key=["agent", "pump", "job", "cli", "other"].index
        )
        assert {"group": "cli", "key": "proj", "archived": 0} in body["sources"]
