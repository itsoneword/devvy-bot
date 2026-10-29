"""Jobs = log; batch digest in the agent chat; duplicate-job guard (dv-c41e)."""

import importlib.util
import json
import os
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.events.bus import EventBus
from src.events.types import AgentResponseEvent
from src.notifications.routing import ReportsRouter
from src.notifications.service import NotificationService
from src.utils.constants import DEFAULT_REPORTS_ROUTES
from tests.unit.test_notifications.test_reports_routing import FakeRepo

OWNER = 100
GROUP = -1004290210285
JOB = "20261022-100000-demo"
REF = f"job:{JOB}"


class FakeLedger:
    def __init__(self, ids: list) -> None:
        self.ids = ids
        self.calls: list = []

    async def latest_for_ref(self, chat_id, ref, kind="report", min_count=1):
        self.calls.append((chat_id, ref, kind, min_count))
        if len(self.ids) < min_count:
            return None
        return SimpleNamespace(telegram_message_id=self.ids[-1])

    async def record(self, **kw: Any) -> None:
        self.ids.append(kw["telegram_message_id"])


def make(ledger, bindings=None):
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(
        message_id=900, chat=SimpleNamespace(id=GROUP)
    )
    bot.create_forum_topic = AsyncMock(
        side_effect=lambda **kw: SimpleNamespace(message_thread_id=31)
    )
    router = ReportsRouter(
        GROUP,
        dict(DEFAULT_REPORTS_ROUTES),
        FakeRepo(),
        [OWNER],
        bindings=bindings or {"Jobs": "devops", "Portuguese": "pl", "Pump": "devops"},
        reply_only=["Jobs", "Pump"],
    )
    svc = NotificationService(
        event_bus=EventBus(),
        bot=bot,
        default_chat_ids=[OWNER],
        sent_messages=ledger,
        reports_router=router,
    )
    return svc, bot


def digest(**kw: Any) -> AgentResponseEvent:
    return AgentResponseEvent(
        chat_id=0,
        text="digest **ok**",
        parse_mode="markdown",
        source="bgjob:digest",
        deliver_profile="pl",
        ledger_profile_slug="pl",
        **kw,
    )


async def test_digest_goes_to_agent_topic_not_jobs() -> None:
    svc, bot = make(FakeLedger([]))
    await svc._deliver(OWNER, digest())
    # Portuguese topic (thread 31 from the fake create), never an edit
    bot.edit_message_text.assert_not_called()
    kw = bot.send_message.call_args.kwargs
    assert kw["chat_id"] == GROUP and kw["message_thread_id"] == 31
    assert svc.sent_messages.ids == [900]  # ledgered, no ref


async def test_digest_dm_when_agent_has_no_topic_and_reply_maps_to_agent() -> None:
    ledger = FakeLedger([])
    ledger.rows = []

    async def record(**kw: Any) -> None:
        ledger.rows.append(kw)

    ledger.record = record  # type: ignore[method-assign]
    svc, bot = make(ledger, bindings={"Jobs": "devops"})
    await svc._deliver(OWNER, digest())
    assert bot.send_message.call_args.kwargs["chat_id"] == OWNER
    assert "message_thread_id" not in bot.send_message.call_args.kwargs
    assert ledger.rows[0]["kind"] == "scheduled_reminder"
    assert ledger.rows[0]["profile_slug"] == "pl"


async def test_plain_job_report_still_goes_to_jobs() -> None:
    svc, bot = make(FakeLedger([]))
    ev = AgentResponseEvent(chat_id=0, text="✅ done", source="bgjob")
    await svc._deliver(OWNER, ev)
    jobs_thread = bot.send_message.call_args.kwargs["message_thread_id"]
    bot.send_message.reset_mock()
    await svc._deliver(OWNER, digest())
    assert bot.send_message.call_args.kwargs["message_thread_id"] is not None
    assert jobs_thread is not None


def test_profile_topic_skips_reply_only_and_report_topics() -> None:
    r = ReportsRouter(
        GROUP,
        dict(DEFAULT_REPORTS_ROUTES),
        FakeRepo(),
        [OWNER],
        bindings={
            "Jobs": "devops",
            "Pump": "devops",
            "Monitoring": "devops",
            "Portuguese": "pl",
        },
        reply_only=["Jobs", "Pump"],
    )
    assert r.profile_topic("devops") is None
    assert r.profile_topic("pl") == "Portuguese"
    assert r.profile_topic(None) is None


# --- duplicate guard ---------------------------------------------------------


@pytest.fixture
def bgjob(tmp_path, monkeypatch):
    path = Path(__file__).parents[2] / "scripts" / "bgjob"
    loader = SourceFileLoader("bgjob_dup_under_test", str(path))
    spec = importlib.util.spec_from_loader("bgjob_dup_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    monkeypatch.setattr(mod, "JOBS_ROOT", tmp_path)
    monkeypatch.setattr(mod, "unit_active", lambda unit: False)
    return mod


def _job(root: Path, jid: str, *, rc=None, age_s=0, **meta: Any) -> None:
    d = root / jid
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"id": jid, "unit": f"u-{jid}", **meta}))
    if rc is not None:
        ec = d / "exit_code"
        ec.write_text(str(rc))
        t = time.time() - age_s
        os.utime(ec, (t, t))


def test_dup_same_name_recent_finished(bgjob, tmp_path) -> None:
    _job(tmp_path, "20261022-1-a", rc=0, age_s=300, name="audit")
    msg = bgjob.find_duplicate("audit", None)
    assert "20261022-1-a" in msg and "finished 5 min ago" in msg


def test_dup_same_task_other_name(bgjob, tmp_path) -> None:
    _job(tmp_path, "20261022-1-a", rc=0, age_s=60, name="x", task="dv-66b4")
    assert "same task dv-66b4" in bgjob.find_duplicate("other", "dv-66b4")


def test_dup_running(bgjob, tmp_path, monkeypatch) -> None:
    _job(tmp_path, "20261022-1-a", name="audit")
    monkeypatch.setattr(bgjob, "unit_active", lambda unit: True)
    assert "RUNNING" in bgjob.find_duplicate("audit", None)


def test_old_failed_cmd_and_other_name_do_not_block(bgjob, tmp_path) -> None:
    _job(tmp_path, "old", rc=0, age_s=3600, name="audit")
    _job(tmp_path, "failed", rc=1, age_s=10, name="audit")
    _job(tmp_path, "cmd", rc=0, age_s=10, name="audit", cmd="pytest")
    _job(tmp_path, "other", rc=0, age_s=10, name="different")
    assert bgjob.find_duplicate("audit", None) == ""


def test_force_flag_and_task_error_text(bgjob) -> None:
    src = (Path(__file__).parents[2] / "scripts" / "bgjob").read_text()
    assert '"--force"' in src
    assert "this repo's" in src and "board" in src


# --- batch digest ------------------------------------------------------------


def _meta_job(root: Path, jid: str, batch: str | None, rc=None, **kw: Any) -> None:
    meta: dict = {"name": jid, "profile": "pl", **kw}
    if batch:
        meta["batch"] = batch
    _job(root, jid, rc=rc, **meta)


def test_plan_digest_rules(bgjob) -> None:
    plan = bgjob.plan_digest
    assert plan([], ["a"], [], None, 0) == ("none",)
    assert plan(["a"], [], [], 0, 1) == ("send", ["a"], [])
    assert plan(["a"], ["b"], [], 0, 60) == ("wait",)
    # cap: 30 min after the first finished job, the finished ones go out
    assert plan(["a"], ["b"], [], 0, bgjob.BATCH_CAP_S) == ("send", ["a"], ["b"])
    # already reported jobs are never repeated; late job gets its own digest
    assert plan(["a", "b"], [], ["a"], None, 5) == ("send", ["b"], [])


@pytest.fixture
def sent_prompts(bgjob, monkeypatch):
    sent: list = []
    monkeypatch.setattr(
        bgjob, "_agentctl_prompt", lambda p, t, n: sent.append((p, t, n)) or True
    )
    monkeypatch.setattr(bgjob, "_spawn_watcher", lambda b: sent.append("watcher"))
    return sent


def test_batch_waits_for_all_then_one_digest(bgjob, tmp_path, sent_prompts) -> None:
    _meta_job(tmp_path, "j1", "B", rc=0)
    _meta_job(tmp_path, "j2", "B")  # still running
    running = lambda u: u == "u-j2"  # noqa: E731
    bgjob.unit_active = running
    assert bgjob.digest_batch("B", now=1000.0) == "wait"
    assert sent_prompts == ["watcher"]
    # j2 finishes: one digest covering both, with the no-re-ask instruction
    (tmp_path / "j2" / "exit_code").write_text("1")
    assert bgjob.digest_batch("B", now=1100.0) == "sent"
    profile, text, name = sent_prompts[1]
    assert profile == "pl" and name == "job-finish:B"
    assert "j1" in text and "j2" in text and "FAILED (exit code 1)" in text
    assert "do NOT ask again" in text
    # a second check (watcher / other job) sends nothing
    assert bgjob.digest_batch("B", now=1200.0) == "none"
    assert len(sent_prompts) == 2


def test_batch_cap_sends_finished_and_names_running(
    bgjob, tmp_path, sent_prompts
) -> None:
    _meta_job(tmp_path, "j1", "B", rc=0)
    _meta_job(tmp_path, "j2", "B")
    bgjob.unit_active = lambda u: u == "u-j2"
    assert bgjob.digest_batch("B", now=0.0) == "wait"
    assert bgjob.digest_batch("B", now=bgjob.BATCH_CAP_S + 1) == "sent"
    text = sent_prompts[-1][1]
    assert "Still running" in text and "j2" in text
    # the late job later gets its own digest, not a repeat of j1
    (tmp_path / "j2" / "exit_code").write_text("0")
    assert bgjob.digest_batch("B", now=bgjob.BATCH_CAP_S + 500) == "sent"
    late = sent_prompts[-1][1]
    assert "j2" in late and '"j1"' not in late


def test_single_job_without_batch_digests_alone(bgjob, tmp_path, sent_prompts) -> None:
    _meta_job(tmp_path, "solo", None, rc=0)
    assert bgjob.digest_batch("solo", now=1.0) == "sent"
    assert len(sent_prompts) == 1


def test_cmd_and_quiet_jobs_never_join_a_batch(bgjob, tmp_path, sent_prompts) -> None:
    _meta_job(tmp_path, "c", "B", rc=0, cmd="pytest")
    _meta_job(tmp_path, "q", "B", rc=0, quiet=True)
    assert bgjob.digest_batch("B", now=1.0) == "none"
    assert sent_prompts == []


def test_failed_send_is_retried(bgjob, tmp_path, monkeypatch) -> None:
    _meta_job(tmp_path, "solo", None, rc=0)
    monkeypatch.setattr(bgjob, "_agentctl_prompt", lambda *a: False)
    assert bgjob.digest_batch("solo", now=1.0) == "failed"
    monkeypatch.setattr(bgjob, "_agentctl_prompt", lambda *a: True)
    assert bgjob.digest_batch("solo", now=2.0) == "sent"


def test_run_sh_calls_digest_not_agentctl_prompt() -> None:
    src = (Path(__file__).parents[2] / "scripts" / "bgjob").read_text()
    assert 'f"digest {shlex.quote(job_id)} "' in src
    assert "--name job-finish:" not in src


def test_job_start_passes_turn_id_as_batch() -> None:
    from src.claude.tool_context import TurnContext

    a, b = TurnContext(), TurnContext()
    assert a.turn_id and a.turn_id != b.turn_id
    src = (Path(__file__).parents[2] / "src/claude/agent_tools.py").read_text()
    assert '"--batch",\n        ctx.turn_id,' in src


# --- Jobs reply is answered in the agent chat --------------------------------


def _orch() -> Any:
    from src.bot.orchestration.turns import TurnsMixin

    o = TurnsMixin.__new__(TurnsMixin)
    o._agent_id_for = lambda *a, **k: "pl"  # type: ignore[attr-defined]
    return o


def _router(bindings=None) -> ReportsRouter:
    class R(ReportsRouter):
        async def topic_name_for_thread(self, chat_id, thread_id):
            return {7: "Jobs", 31: "Portuguese"}.get(thread_id)

        async def thread_id(self, bot, topic, recreate=False):
            return 31

        async def profile_for_message(self, message):
            return "pl"

    return R(
        GROUP,
        dict(DEFAULT_REPORTS_ROUTES),
        FakeRepo(),
        [OWNER],
        bindings=bindings or {"Jobs": "devops", "Portuguese": "pl"},
        reply_only=["Jobs"],
    )


def _update(thread: int = 7, text: str = "а что с ошибкой?") -> Any:
    msg = AsyncMock()
    msg.chat_id = GROUP
    msg.message_id = 55
    msg.message_thread_id = thread
    msg.is_topic_message = True
    msg.text = text
    msg.caption = None
    msg.reply_to_message = SimpleNamespace(
        chat=SimpleNamespace(id=GROUP), message_id=40
    )
    return SimpleNamespace(message=msg, effective_user=SimpleNamespace(id=OWNER))


def _state(router: ReportsRouter) -> Any:
    from src.bot.utils.formatting import FormattedMessage  # noqa: F401

    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(
        message_id=901, chat=SimpleNamespace(id=GROUP)
    )
    recorded: list = []

    class Sent:
        async def lookup(self, chat_id, message_id):
            return SimpleNamespace(ref=REF, kind="report", profile_slug="pl")

        async def record(self, **kw: Any) -> None:
            recorded.append(kw)

    ctx = SimpleNamespace(
        bot=bot,
        bot_data={
            "reports_router": router,
            "storage": SimpleNamespace(sent_messages=Sent()),
        },
    )
    state = SimpleNamespace(
        context=ctx,
        request=SimpleNamespace(resilient_send=True),
        saved_message_id=1,
        current_dir="/x",
        user_id=OWNER,
    )
    return state, bot, recorded


async def test_jobs_reply_answered_in_agent_topic_with_pointer() -> None:
    from src.bot.utils.formatting import FormattedMessage

    router = _router()
    state, bot, recorded = _state(router)
    upd = _update()
    await _orch()._deliver_reply(state, upd, [FormattedMessage("ответ", "HTML")], [])
    kw = bot.send_message.call_args.kwargs
    assert kw["chat_id"] == GROUP and kw["message_thread_id"] == 31
    assert kw["text"].startswith("↩ в Jobs: «а что с ошибкой?»")
    assert recorded[0]["ref"] is None  # answer is not a job message
    upd.message.reply_text.assert_awaited_once()
    pointer = upd.message.reply_text.call_args
    assert "→ ответ в чате" in pointer.args[0]
    assert "t.me/c/4290210285/31/901" in pointer.args[0]


async def test_jobs_reply_dm_when_agent_has_no_topic() -> None:
    from src.bot.utils.formatting import FormattedMessage

    router = _router(bindings={"Jobs": "devops"})
    state, bot, _ = _state(router)
    upd = _update()
    await _orch()._deliver_reply(state, upd, [FormattedMessage("ответ", None)], [])
    kw = bot.send_message.call_args.kwargs
    assert kw["chat_id"] == OWNER and kw["message_thread_id"] is None
    assert "личке" in upd.message.reply_text.call_args.args[0]


async def test_reply_outside_reply_only_topic_answers_in_place() -> None:
    from src.bot.utils.formatting import FormattedMessage

    router = _router()
    state, bot, recorded = _state(router)
    upd = _update(thread=31)  # the agent's own topic
    await _orch()._deliver_reply(state, upd, [FormattedMessage("ответ", None)], [])
    bot.send_message.assert_not_called()
    assert upd.message.reply_text.call_args.args[0] == "ответ"
    assert recorded[0]["ref"] == REF  # unchanged behaviour
