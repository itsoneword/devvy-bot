"""Replies in the reply-only Jobs topic (dv-c41e)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ApplicationHandlerStop

from src.bot.core import ClaudeCodeBot
from src.bot.orchestration.job_context import describe_job, job_envelope
from src.bot.orchestration.reply_context import build_reply_context
from src.notifications.routing import HINT_INTERVAL_S, ReportsRouter
from src.storage.database import DatabaseManager
from src.storage.sent_messages import SentMessageRepository
from tests.unit.test_notifications.test_reports_routing import FakeRepo

GROUP = -1004290210285
OWNER = 100
BOT_ID = 7
JOBS_THREAD = 31
JOB = "20261017-000000-demo"


class FakeLedger:
    def __init__(self, rows):
        self.rows = rows

    async def lookup(self, chat_id, message_id):
        return self.rows.get((chat_id, message_id))


def _entry(ref, profile="devops"):
    return SimpleNamespace(
        kind="report",
        ref=ref,
        profile_slug=profile,
        created_at=None,
        messages_message_id=None,
    )


def _router(policy="owner", rows=None):
    repo = FakeRepo()
    repo.rows[(GROUP, "reports:jobs")] = SimpleNamespace(
        message_thread_id=JOBS_THREAD, is_active=True
    )
    repo.rows[(GROUP, "reports:coach")] = SimpleNamespace(
        message_thread_id=21, is_active=True
    )
    ledger = FakeLedger(
        rows
        if rows is not None
        else {
            (GROUP, 500): _entry(f"job:{JOB}", "devops"),  # start
            (GROUP, 501): _entry(f"job:{JOB}", "chef"),  # finish
            (GROUP, 502): _entry(None, None),  # pin / old message
        }
    )
    return ReportsRouter(
        GROUP,
        {},
        repo,
        [OWNER],
        bindings={"Jobs": "devops", "Coach": "coach"},
        inbound_policy=policy,
        reply_only=["Jobs"],
        ledger=ledger,
    )


def _m(thread=JOBS_THREAD, reply_to=None, text="hi", uid=OWNER):
    return SimpleNamespace(
        chat_id=GROUP,
        text=text,
        message_thread_id=thread,
        is_topic_message=thread is not None,
        reply_to_message=(
            SimpleNamespace(chat=SimpleNamespace(id=GROUP), message_id=reply_to)
            if reply_to is not None
            else None
        ),
        reply_text=AsyncMock(),
    )


async def test_reply_to_start_and_finish_routes_to_ledger_profile():
    r = _router()
    assert await r.profile_for_message(_m(reply_to=500)) == "devops"
    assert await r.profile_for_message(_m(reply_to=501)) == "chef"


async def test_reply_to_agent_answer_that_inherited_job_ref_routes():
    # The agent's answer in Jobs is ledgered as turn_reply with the job's ref
    # (turns.py), so the owner can continue the thread by replying to it.
    answer = _entry(f"job:{JOB}", "devops")
    answer.kind = "turn_reply"
    r = _router(rows={(GROUP, 600): answer, (GROUP, 601): _entry(None, "devops")})
    assert await r.profile_for_message(_m(reply_to=600)) == "devops"
    assert await r.profile_for_message(_m(reply_to=601)) is None


async def test_non_job_targets_are_unrouted():
    r = _router()
    assert await r.profile_for_message(_m(reply_to=502)) is None  # no ref
    assert await r.profile_for_message(_m(reply_to=999)) is None  # unledgered
    assert await r.profile_for_message(_m(reply_to=None)) is None  # plain post


async def test_normal_topic_keeps_static_binding():
    assert await _router().profile_for_message(_m(thread=21)) == "coach"


def _gate(router, role="owner"):
    core = ClaudeCodeBot.__new__(ClaudeCodeBot)
    core.settings = SimpleNamespace(reports_chat_id=GROUP, allowed_users=[OWNER])
    core.deps = {
        "reports_router": router,
        "auth_manager": SimpleNamespace(get_user_role=lambda uid: role),
    }
    core.app = MagicMock()
    core._add_reports_chat_handlers()
    handlers = [c.args[0] for c in core.app.add_handler.call_args_list]
    return next(h for h in handlers if hasattr(h, "filters")).callback


def _upd(m, uid=OWNER):
    return SimpleNamespace(effective_message=m, effective_user=SimpleNamespace(id=uid))


async def test_gate_passes_owner_job_reply():
    m = _m(reply_to=500)
    assert await _gate(_router())(_upd(m), MagicMock()) is None
    m.reply_text.assert_not_called()


async def test_gate_non_owner_blocked_even_with_allowed_policy():
    m = _m(reply_to=500, uid=5)
    with pytest.raises(ApplicationHandlerStop):
        await _gate(_router("allowed"), role="guest")(_upd(m, 5), MagicMock())
    m.reply_text.assert_not_called()


async def test_gate_hint_is_throttled():
    r = _router()
    gate = _gate(r)
    first, second = _m(reply_to=None), _m(reply_to=502)
    for m in (first, second):
        with pytest.raises(ApplicationHandlerStop):
            await gate(_upd(m), MagicMock())
    first.reply_text.assert_awaited_once()
    second.reply_text.assert_not_called()


def test_hint_due_again_after_interval():
    r = _router()
    assert r.hint_due("Jobs", now=0.0)
    assert not r.hint_due("Jobs", now=HINT_INTERVAL_S - 1)
    assert r.hint_due("Jobs", now=HINT_INTERVAL_S + 1)


# --- envelope ----------------------------------------------------------------


def _job(tmp_path, code=None, output=""):
    d = tmp_path / JOB
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"name": "demo", "profile": "devops"}))
    if code is not None:
        (d / "exit_code").write_text(f"{code}\n")
    (d / "output.md").write_text(output)


def test_describe_job_states(tmp_path):
    _job(tmp_path)
    assert describe_job(JOB, tmp_path)["status"] == "running"
    (tmp_path / JOB / "exit_code").write_text("0")
    assert describe_job(JOB, tmp_path)["status"] == "finished"
    (tmp_path / JOB / "exit_code").write_text("2")
    assert describe_job(JOB, tmp_path)["status"] == "FAILED rc=2"


def test_envelope_truncates_head_and_never_inlines_tail(tmp_path):
    lines = [f"line{i}" for i in range(40)]
    _job(tmp_path, 0, "\n".join(lines))
    env = job_envelope(JOB, tmp_path)
    assert "line14" in env and "line15" not in env and "line39" not in env
    assert "untrusted" in env and "output.md" in env and JOB in env
    _job_long = tmp_path / JOB / "output.md"
    _job_long.write_text("x" * 5000)
    assert len(job_envelope(JOB, tmp_path)) < 1700


def test_envelope_missing_job_and_bad_id(tmp_path):
    assert "not found" in job_envelope(JOB, tmp_path)
    assert not describe_job("../etc", tmp_path)["found"]


async def test_reply_context_uses_job_envelope(tmp_path, monkeypatch):
    _job(tmp_path, 0, "result A\nresult B")
    monkeypatch.setenv("BGJOB_ROOT", str(tmp_path))
    quoted = SimpleNamespace(
        text="✅ finished\n" + "tail " * 100,
        caption=None,
        message_id=501,
        chat=SimpleNamespace(id=GROUP, type="supergroup"),
        from_user=SimpleNamespace(id=BOT_ID),
    )
    update = SimpleNamespace(message=SimpleNamespace(reply_to_message=quoted))
    ledger = FakeLedger({(GROUP, 501): _entry(f"job:{JOB}")})
    prompt, user_text = await build_reply_context(
        update,
        BOT_ID,
        ledger,
        "why?",
        viewer_user_id=OWNER,
        viewer_is_owner=True,
        reports_chat_id=GROUP,
    )
    assert user_text == "why?"
    assert "result A" in prompt and "tail tail" not in prompt
    assert prompt.endswith("why?")


# --- ledger ref round trip ---------------------------------------------------


async def test_ledger_ref_round_trip(tmp_path):
    db = DatabaseManager(f"sqlite:///{tmp_path}/b.db")
    await db.initialize()
    repo = SentMessageRepository(db)
    await repo.record(GROUP, 1, "report", profile_slug="devops", ref=f"job:{JOB}")
    await repo.record(GROUP, 2, "report")
    assert (await repo.lookup(GROUP, 1)).ref == f"job:{JOB}"
    assert (await repo.lookup(GROUP, 2)).ref is None


def test_digest_name_with_job_id_keeps_digest_source():
    from src.events.handlers import scheduled_report_source
    from src.events.types import ScheduledEvent

    assert scheduled_report_source(ScheduledEvent(job_name=f"job-finish:{JOB}")) == (
        "bgjob:digest"
    )
    assert scheduled_report_source(ScheduledEvent(job_name="job-finish")) == (
        "bgjob:digest"
    )


def test_notify_request_carries_ref_and_profile():
    from src.api.server import NotifyRequest

    n = NotifyRequest(text="x", ref=f"job:{JOB}", profile="devops")
    assert (n.ref, n.profile) == (f"job:{JOB}", "devops")


# --- pump question cards (2026-10-20) ----------------------------------------


def test_pump_envelope_names_task_and_answer_command():
    from src.bot.orchestration.job_context import pump_envelope, pump_task_from_ref

    assert pump_task_from_ref("pump:name:dv-1") == "name:dv-1"
    assert pump_task_from_ref("job:x") is None
    env = pump_envelope("name:dv-1")
    # dv-a9b0: the card may sit in any board agent's chat, and sandboxed
    # agents cannot run the script -> name the tools, not scripts/taskpump.
    assert "pump_pending" in env and "pump_answer" in env
    assert "name:dv-1" in env and "verbatim" in env
    assert "scripts/taskpump" not in env and "in Pump" not in env


async def test_reply_context_uses_pump_envelope():
    quoted = SimpleNamespace(
        text="🔍 dv-1 · t\n1. q?",
        caption=None,
        message_id=601,
        chat=SimpleNamespace(id=GROUP, type="supergroup"),
        from_user=SimpleNamespace(id=BOT_ID),
    )
    update = SimpleNamespace(message=SimpleNamespace(reply_to_message=quoted))
    ledger = FakeLedger({(GROUP, 601): _entry("pump:dv-1")})
    prompt, user_text = await build_reply_context(
        update,
        BOT_ID,
        ledger,
        "1 default",
        viewer_user_id=OWNER,
        viewer_is_owner=True,
        reports_chat_id=GROUP,
    )
    assert user_text == "1 default"
    assert "pump_answer" in prompt and "dv-1" in prompt
    assert prompt.endswith("1 default")


def test_reply_only_accepts_pump_ref():
    from src.notifications import routing

    assert "pump:" in routing.REPLY_REF_PREFIXES


# --- pump cards in a board agent's own chat (dv-a9b0) --------------------------

GYMBRO_THREAD = 41


def _pump_quote(message_id, chat_id=GROUP, chat_type="supergroup"):
    quoted = SimpleNamespace(
        text="🔍 gymbro:dv-1 · t\n1. q?",
        caption=None,
        message_id=message_id,
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=SimpleNamespace(id=BOT_ID),
    )
    return SimpleNamespace(message=SimpleNamespace(reply_to_message=quoted))


async def test_reply_to_gymbro_card_runs_on_gymbro_with_pump_tools():
    """Scenario 1, reply half: the owner replies to a pump card delivered to
    the GymBro topic; the turn runs on gymbro and its envelope names the
    pump tools for the task."""
    repo = FakeRepo()
    repo.rows[(GROUP, "reports:gymbro")] = SimpleNamespace(
        message_thread_id=GYMBRO_THREAD, is_active=True
    )
    ledger = FakeLedger({(GROUP, 701): _entry("pump:gymbro:dv-1", "gymbro")})
    router = ReportsRouter(
        GROUP,
        {},
        repo,
        [OWNER],
        bindings={"GymBro": "gymbro", "Pump": "devops"},
        inbound_policy="owner",
        reply_only=["Pump"],
        ledger=ledger,
    )
    m = _m(thread=GYMBRO_THREAD, reply_to=701, text="1 default, 2 да")
    assert await router.profile_for_message(m) == "gymbro"

    prompt, user_text = await build_reply_context(
        _pump_quote(701),
        BOT_ID,
        ledger,
        "1 default, 2 да",
        current_agent_id="gymbro",
        viewer_user_id=OWNER,
        viewer_is_owner=True,
        reports_chat_id=GROUP,
    )
    assert user_text == "1 default, 2 да"
    assert "pump_pending" in prompt and "pump_answer" in prompt
    assert "gymbro:dv-1" in prompt
    assert "scripts/taskpump" not in prompt
    assert prompt.endswith("1 default, 2 да")


async def test_dm_fallback_pump_card_is_not_called_a_scheduled_reminder():
    """No topic for the profile: the card lands in the owner DM, ledgered as
    scheduled_reminder (so a reply reroutes to the profile). The envelope
    must still be the pump one, not 'a scheduled reminder you composed'."""
    entry = _entry("pump:gymbro:dv-1", "gymbro")
    entry.kind = "scheduled_reminder"
    ledger = FakeLedger({(OWNER, 702): entry})
    prompt, _ = await build_reply_context(
        _pump_quote(702, chat_id=OWNER, chat_type="private"),
        BOT_ID,
        ledger,
        "go",
        current_agent_id="gymbro",
        viewer_user_id=OWNER,
        viewer_is_owner=True,
        reports_chat_id=GROUP,
    )
    assert "scheduled reminder" not in prompt
    assert "pump_answer" in prompt and "gymbro:dv-1" in prompt
