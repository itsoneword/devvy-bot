"""dv-d2a3 phase 2: the Overview UI shows sessions by attributed source.

These tests drive the real frontend entry point (``static/app.js`` ->
``views/overview.js``) in node, with a small DOM shim
(``ui_harness.mjs``), against the real API served by uvicorn over an index
built by the real indexer from a fixture world (the phase-1 ``World``).

Contract the UI must meet (analysis §4 "API / UI", .pump/PHASES.md phase 2):

- The Overview has a card whose ``<h2>`` contains "source" (e.g. "By source").
  Its top level lists every live agent (profiles.yaml, enabled) and the
  groups "Pump", "Jobs", an owner-CLI row (text contains "CLI") and "Other"
  (or "Прочее"), in that order, each row with its est-$ cost.
- The Pump row expands (click on the row or a control in it, or a
  ``<details>``/``<summary>``) to boards, a board row expands to its tasks,
  and the stages of a task are shown (visible text, a ``title`` tooltip, or
  one more expand level).
- Archived agents (disabled / unknown slugs, ``archived`` = 1) are not shown
  by default. A toggle (checkbox or button labelled "Archive"/"Архив")
  shows them. Hiding = not rendered, or ``hidden`` / ``display:none`` /
  inside a closed ``<details>``.
- A source ``<select>`` in the filter bar: choosing a source sends the same
  filter to /api/overview (tiles + daily chart) and /api/sessions (table).
- The sessions table has a "Source" column with the attributed source
  (``Pump · gymbro · gb-1a72 · implement-p1`` style), not the raw cwd label.
- ``?profile=<slug>`` on the API keeps filtering to that agent.
"""

import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest
import uvicorn
from fastapi.testclient import TestClient

from src.llm_analytics.config import AnalyticsConfig
from src.llm_analytics.db import connect, init_db
from src.llm_analytics.indexer import Indexer
from src.llm_analytics.pricing import PricingTable
from src.llm_analytics.server import build_app
from tests.unit.test_llm_analytics.test_attribution import USER, World, _sid
from tests.unit.test_llm_analytics.test_indexer import PRICING

REPO = Path(__file__).resolve().parents[3]
STATIC_DIR = REPO / "src" / "llm_analytics" / "static"
HARNESS = Path(__file__).with_name("ui_harness.mjs")
NODE = shutil.which("node")

# Fixture sessions, by the source they must be attributed to.
DEVOPS_CHAT = _sid(101)  # agent/devops (bot chat in the repo dir)
COACH_CHAT = _sid(102)  # agent/coach
COACH_CHAT_2 = _sid(103)  # agent/coach
PM_CHAT = _sid(104)  # agent/pm, archived (disabled profile)
PUMP_GB_TEST = _sid(110)  # pump/gymbro gb-1a72/testdesign-p1
PUMP_GB_IMPL = _sid(111)  # pump/gymbro gb-1a72/implement-p1
PUMP_DV_BUILD = _sid(112)  # pump/devvy dv-0ecd/build-p1
JOB_MONITOR = _sid(120)  # job/server-monitor
CLI_OWNER = _sid(130)  # cli/int_prep
SMOKE = _sid(140)  # other/tests
INTERNAL = _sid(141)  # other/devvy-internal

TOP_GROUPS = ["Pump", "Jobs", "CLI", ("Other", "Прочее")]


# ----------------------------------------------------------------------
# Fixture world -> index -> live server
# ----------------------------------------------------------------------


def _populate(world: World) -> None:
    world.bot_session(DEVOPS_CHAT, world.repo)
    world.transcript(DEVOPS_CHAT, world.repo, start="2026-10-20T09:00:00", calls=3)

    coach_cwd = world.coach_wd / "users" / str(USER)
    coach_cwd.mkdir(parents=True)
    world.bot_session(COACH_CHAT, coach_cwd)
    world.transcript(COACH_CHAT, coach_cwd, start="2026-10-20T10:00:00", calls=2)
    world.bot_session(COACH_CHAT_2, coach_cwd)
    world.transcript(COACH_CHAT_2, coach_cwd, start="2026-10-21T10:00:00")

    world.bot_session(PM_CHAT, world.pm_wd)
    world.transcript(PM_CHAT, world.pm_wd, start="2026-10-20T11:00:00")

    gb = world.worktrees / "gymbro" / "gb-1a72"
    gb.mkdir(parents=True)
    world.job(
        "20261024-074853-pump-gymbro-gb-1a72-testdesign-p1",
        "pump-gymbro-gb-1a72-testdesign-p1",
        cwd=gb,
        created="2026-10-24T07:48:53+00:00",
        profile="gymbro",
        session_id=PUMP_GB_TEST,
    )
    world.transcript(PUMP_GB_TEST, gb, start="2026-10-24T07:49:03", calls=2)
    world.job(
        "20261024-075953-pump-gymbro-gb-1a72-implement-p1",
        "pump-gymbro:gb-1a72-implement-p1",
        cwd=gb,
        created="2026-10-24T07:59:53+00:00",
        profile="gymbro",
        session_id=PUMP_GB_IMPL,
    )
    world.transcript(PUMP_GB_IMPL, gb, start="2026-10-24T08:00:03", calls=4)

    dv = world.worktrees / "dv-0ecd"
    dv.mkdir(parents=True)
    world.job(
        "20261023-100000-pump-dv-0ecd-build-p1",
        "pump-dv-0ecd-build-p1",
        cwd=dv,
        created="2026-10-23T10:00:00+00:00",
        session_id=PUMP_DV_BUILD,
    )
    world.transcript(PUMP_DV_BUILD, dv, start="2026-10-23T10:00:05", calls=2)

    world.job(
        "20261022-060000-server-monitor",
        "server-monitor",
        cwd=world.root,
        created="2026-10-22T06:00:00+00:00",
        session_id=JOB_MONITOR,
    )
    world.model_call(JOB_MONITOR, "monitor", ts="2026-10-22 06:00:10")
    world.transcript(JOB_MONITOR, world.root, start="2026-10-22T06:00:05")

    cli_cwd = world.root / "int_prep" / ".worktrees" / "r1-p10"
    cli_cwd.mkdir(parents=True)
    world.transcript(
        CLI_OWNER, cli_cwd, start="2026-10-21T15:00:00", entrypoint="cli", calls=2
    )

    world.transcript(
        SMOKE,
        "/tmp/backend-smoke-k3j2/data/workspaces/coach/users/900001",
        start="2026-10-21T16:00:00",
    )
    world.model_call(INTERNAL, "extraction", ts="2026-10-21 17:00:00")
    world.transcript(
        INTERNAL, world.repo, start="2026-10-21T17:00:00", entrypoint="sdk-cli"
    )


def _config(world: World) -> AnalyticsConfig:
    pricing_path = world.root / "pricing.json"
    pricing_path.write_text(json.dumps(PRICING))
    return AnalyticsConfig(
        host="127.0.0.1",
        port=0,
        db_path=world.root / "index.db",
        projects_dir=world.projects,
        bot_db_path=world.bot_db,
        pricing_path=pricing_path,
        scan_interval=3600,  # background loop scans once at startup only
        profiles_path=world.profiles_path,
        jobs_dir=world.jobs,
        pump_worktrees_dir=world.worktrees,
        boards_path=world.boards_path,
    )


@pytest.fixture()
def ui_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AnalyticsConfig:
    monkeypatch.setenv("PROFILES_CONFIG_PATH", str(tmp_path / "profiles.yaml"))
    monkeypatch.setenv("BGJOB_ROOT", str(tmp_path / "Devvybot" / "jobs"))
    monkeypatch.setenv("PUMP_WORKTREES", str(tmp_path / "Devvybot" / "pump-worktrees"))
    world = World(tmp_path)
    _populate(world)
    config = _config(world)
    conn = connect(config.db_path)
    init_db(conn)
    pricing = PricingTable.load(config.pricing_path)
    Indexer(conn, config, pricing).scan_once()
    conn.close()
    return config


@pytest.fixture()
def api(ui_world: AnalyticsConfig) -> Iterator[TestClient]:
    with TestClient(build_app(ui_world)) as client:
        yield client


@pytest.fixture()
def server(ui_world: AnalyticsConfig) -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    srv = uvicorn.Server(
        uvicorn.Config(
            build_app(ui_world), host="127.0.0.1", port=port, log_level="error"
        )
    )
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not srv.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/"
    srv.should_exit = True
    thread.join(timeout=10)


# ----------------------------------------------------------------------
# Running the frontend
# ----------------------------------------------------------------------


def _run_ui(base: str, tmp_path: Path, actions: list[dict[str, Any]]) -> dict:
    if NODE is None:
        pytest.skip("node is not installed")
    spec = tmp_path / "ui_spec.json"
    spec.write_text(
        json.dumps(
            {
                "base": base,
                "static_dir": str(STATIC_DIR),
                "out": str(tmp_path / "ui_out.json"),
                # The fixture data is older than the default 7d range.
                "actions": [{"op": "click_button", "text": "all"}, *actions],
            }
        )
    )
    proc = subprocess.run(
        [NODE, str(HARNESS), str(spec)],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads((tmp_path / "ui_out.json").read_text())


def _no_render_errors(out: dict) -> None:
    assert out["errors"] == [], out["errors"]
    for snap in out["snapshots"]:
        text = _text(snap["tree"])
        assert "Failed to render" not in text, text[:500]
        assert "Failed to load overview" not in text, text[:500]


def _snap(out: dict, label: str) -> dict:
    for snap in out["snapshots"]:
        if snap["label"] == label:
            return snap
    raise AssertionError(f"no snapshot {label}")


# -- tree helpers (visibility rules from the module docstring) ----------


def _self_hidden(node: dict) -> bool:
    if node.get("hidden") or "hidden" in node["a"]:
        return True
    style = node["a"].get("style", "").replace(" ", "").lower()
    return "display:none" in style


def _children(node: dict, *, visible: bool = True) -> Iterator[Any]:
    for child in node["c"]:
        if isinstance(child, str):
            yield child
            continue
        if visible and _self_hidden(child):
            continue
        if (
            visible
            and node["t"] == "details"
            and not node.get("open")
            and child["t"] != "summary"
        ):
            continue
        yield child


def _text(node: Any, *, visible: bool = True) -> str:
    if isinstance(node, str):
        return node
    return " ".join(_text(c, visible=visible) for c in _children(node, visible=visible))


def _titles(node: Any) -> str:
    """Visible text plus every title / aria-label (tooltips) in the subtree."""
    if isinstance(node, str):
        return node
    own = " ".join(v for k, v in node["a"].items() if k in ("title", "aria-label"))
    return own + " " + " ".join(_titles(c) for c in _children(node))


def _walk(node: Any, *, visible: bool = True) -> Iterator[dict]:
    if isinstance(node, str):
        return
    yield node
    for child in _children(node, visible=visible):
        yield from _walk(child, visible=visible)


def _has_class(node: dict, cls: str) -> bool:
    return cls in node["a"].get("class", "").split()


def _card(tree: dict, heading: str) -> dict | None:
    for node in _walk(tree):
        if not _has_class(node, "card"):
            continue
        for inner in _walk(node):
            if inner["t"] in ("h2", "h3"):
                if heading.lower() in _text(inner).lower():
                    return node
                break
    return None


def _source_card(tree: dict) -> dict:
    card = _card(tree, "source")
    assert card is not None, "no Overview card with 'source' in its heading"
    return card


def _words(text: str) -> set[str]:
    import re

    return set(re.findall(r"[\w-]+", text.lower()))


def _contains(text: str, label: str | tuple[str, ...]) -> bool:
    labels = label if isinstance(label, tuple) else (label,)
    return any(lab.lower() in text.lower() for lab in labels)


def _pos(text: str, label: str | tuple[str, ...]) -> int:
    labels = label if isinstance(label, tuple) else (label,)
    hits = [text.lower().find(lab.lower()) for lab in labels]
    hits = [h for h in hits if h >= 0]
    return min(hits) if hits else -1


def _row_of(card: dict, label: str | tuple[str, ...], others: list[Any]) -> str:
    """Text of the smallest element holding ``label`` and a "$" amount and
    none of the other top-level labels: the label's own row."""
    best: str | None = None
    for node in _walk(card):
        text = _text(node)
        if not _contains(text, label) or "$" not in text:
            continue
        if any(_contains(text, o) for o in others if o != label):
            continue
        if best is None or len(text) < len(best):
            best = text
    assert best is not None, f"no row with a $ cost for {label!r}"
    return best


def _last(requests: list[dict], path: str) -> dict:
    hits = [r for r in requests if r["path"] == path]
    assert hits, f"no request to {path}"
    return hits[-1]


_NON_FILTER = {"limit", "offset", "sort", "order", "from", "to", "range"}


def _filter_params(req: dict) -> dict:
    return {k: v for k, v in req["params"].items() if k not in _NON_FILTER}


# ----------------------------------------------------------------------
# Approved scenarios (binding)
# ----------------------------------------------------------------------


class TestOverviewBySource:
    def test_top_level_lists_live_agents_pump_jobs_cli_other_with_cost(
        self, server: str, tmp_path: Path
    ) -> None:
        out = _run_ui(server, tmp_path, [{"op": "snapshot", "label": "default"}])
        _no_render_errors(out)
        card = _source_card(_snap(out, "default")["tree"])
        text = _text(card)

        # Live agents from profiles.yaml, then the four non-agent groups.
        for slug in ("devops", "coach"):
            assert slug in text.lower(), f"live agent {slug} missing: {text}"
        positions = [_pos(text, g) for g in TOP_GROUPS]
        assert all(p >= 0 for p in positions), (TOP_GROUPS, text)
        assert positions == sorted(positions), f"group order wrong: {text}"
        assert max(_pos(text, "devops"), _pos(text, "coach")) < positions[0]

        # Each top-level row carries its own est-$ cost.
        tops: list[Any] = ["devops", "coach", *TOP_GROUPS]
        for label in tops:
            _row_of(card, label, tops)

        # No raw cwd label: the repo-dir chat is devops, not devvy_bot.
        assert "devvy_bot" not in text

    def test_pump_row_expands_to_boards_then_tasks_with_stages(
        self, server: str, tmp_path: Path
    ) -> None:
        out = _run_ui(
            server,
            tmp_path,
            [
                {"op": "expand", "label": "Pump", "until": "gymbro"},
                {"op": "snapshot", "label": "boards"},
                {"op": "expand", "label": "gymbro", "until": "gb-1a72"},
                {"op": "snapshot", "label": "tasks"},
                {"op": "expand", "label": "gb-1a72", "until": "implement-p1"},
                {"op": "snapshot", "label": "stages"},
            ],
        )
        _no_render_errors(out)

        boards = _text(_source_card(_snap(out, "boards")["tree"]))
        assert "gymbro" in boards and "devvy" in boards, boards
        assert _pos(boards, "Pump") < _pos(boards, "gymbro")

        tasks = _text(_source_card(_snap(out, "tasks")["tree"]))
        assert "gb-1a72" in tasks, tasks
        assert _pos(tasks, "gymbro") < _pos(tasks, "gb-1a72")

        stages_card = _source_card(_snap(out, "stages")["tree"])
        stages = _titles(stages_card)
        assert "implement-p1" in stages and "testdesign-p1" in stages, stages
        # Tasks never surface as top-level agents: the task row sits under
        # its board, after the Pump row.
        assert _pos(_text(stages_card), "Pump") < _pos(_text(stages_card), "gb-1a72")

    def test_archive_hidden_by_default_toggle_shows_pm(
        self, server: str, tmp_path: Path
    ) -> None:
        out = _run_ui(
            server,
            tmp_path,
            [
                {"op": "snapshot", "label": "default"},
                {"op": "toggle_archive"},
                {"op": "snapshot", "label": "archive"},
            ],
        )
        _no_render_errors(out)

        default = _text(_source_card(_snap(out, "default")["tree"]))
        assert "coach" in default.lower()
        assert "pm" not in _words(default), default

        shown = _text(_source_card(_snap(out, "archive")["tree"]))
        assert "pm" in _words(shown), shown
        assert "coach" in shown.lower()

    def test_source_filter_narrows_tiles_daily_and_sessions_together(
        self, server: str, tmp_path: Path
    ) -> None:
        out = _run_ui(
            server,
            tmp_path,
            [
                {"op": "snapshot", "label": "all"},
                {"op": "select_source", "match": "gymbro"},
                {"op": "snapshot", "label": "filtered"},
            ],
        )
        _no_render_errors(out)
        before = _snap(out, "all")
        after = _snap(out, "filtered")
        new_requests = after["requests"][len(before["requests"]) :]

        overview = _last(new_requests, "api/overview")
        sessions = _last(new_requests, "api/sessions")
        # One filter, sent to the tiles/daily source and the table alike.
        assert _filter_params(overview), overview["params"]
        assert _filter_params(overview) == _filter_params(sessions)

        # The data behind tiles + daily chart is narrowed to gymbro's pump.
        all_total = _last(before["requests"], "api/overview")["json"]["totals"]
        totals = overview["json"]["totals"]
        assert totals["sessions"] == 2
        assert totals["sessions"] < all_total["sessions"]
        daily_calls = sum(d["api_calls"] for d in overview["json"]["daily"])
        assert daily_calls == totals["api_calls"] == 6

        rows = sessions["json"]["sessions"]
        assert {s["session_id"] for s in rows} == {PUMP_GB_TEST, PUMP_GB_IMPL}

        # And the page shows it: Sessions tile and table row count.
        tree = after["tree"]
        tile_values = {}
        for node in _walk(tree):
            if _has_class(node, "tile"):
                parts = [c for c in _walk(node) if c is not node]
                label = next(
                    (_text(p) for p in parts if _has_class(p, "tile-label")), ""
                )
                value = next(
                    (_text(p) for p in parts if _has_class(p, "tile-value")), ""
                )
                tile_values[label.strip()] = value.strip()
        assert tile_values.get("Sessions") == "2", tile_values
        table_card = _card(tree, "Sessions")
        assert table_card is not None
        body_rows = [
            n
            for n in _walk(table_card)
            if n["t"] == "tr" and not any(c["t"] == "th" for c in _walk(n))
        ]
        assert len(body_rows) == 2

    def test_old_profile_link_still_filters_to_that_agent(
        self, api: TestClient
    ) -> None:
        old = api.get("/api/overview", params={"profile": "coach"}).json()
        new = api.get("/api/overview", params={"source": "agent/coach"}).json()
        assert old["totals"] == new["totals"]
        assert old["totals"]["sessions"] == 2
        assert old["totals"]["api_calls"] == 3

        page = api.get("/api/sessions", params={"profile": "coach"}).json()
        assert {s["session_id"] for s in page["sessions"]} == {
            COACH_CHAT,
            COACH_CHAT_2,
        }
        # An archived agent's old link still works too.
        page = api.get("/api/sessions", params={"profile": "pm"}).json()
        assert [s["session_id"] for s in page["sessions"]] == [PM_CHAT]

    # ------------------------------------------------------------------
    # Added scenarios
    # ------------------------------------------------------------------

    def test_sessions_table_source_column_shows_attributed_source(
        self, server: str, tmp_path: Path
    ) -> None:
        """added: the table's Profile column becomes Source and reads the
        attributed source (Pump · gymbro · gb-1a72 · implement-p1), never the
        raw cwd label (gb-1a72 / devvy_bot)."""
        out = _run_ui(server, tmp_path, [{"op": "snapshot", "label": "default"}])
        _no_render_errors(out)
        table_card = _card(_snap(out, "default")["tree"], "Sessions")
        assert table_card is not None
        headers = [_text(n).strip() for n in _walk(table_card) if n["t"] == "th"]
        assert any(h.startswith("Source") for h in headers), headers
        assert "Profile" not in headers

        rows = [
            _text(n)
            for n in _walk(table_card)
            if n["t"] == "tr" and not any(c["t"] == "th" for c in _walk(n))
        ]
        impl = [r for r in rows if PUMP_GB_IMPL[:8] in r]
        assert impl, rows
        for word in ("pump", "gymbro", "gb-1a72", "implement-p1"):
            assert word in impl[0].lower(), impl[0]
        devops = [r for r in rows if DEVOPS_CHAT[:8] in r]
        assert devops and "devops" in devops[0].lower(), rows
        assert not any("devvy_bot" in r for r in rows), rows
