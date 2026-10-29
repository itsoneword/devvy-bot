"""dv-b6dd, beyond the protected scenario tests: the task tracker's terminal
states, the orphan scan's scope, and the record of a multi-turn job."""

import io
import os
from pathlib import Path

from claude_agent_sdk import (
    ResultMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
)

from src.model_router import job as job_mod
from src.model_router.job import BackgroundTasks, BotLink, nudge_text, orphans


def _started(task_id: str) -> TaskStartedMessage:
    return TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id=task_id,
        description="sleep 40",
        uuid="u",
        session_id="s",
    )


def _updated(task_id: str, status: str, in_patch: bool = False) -> TaskUpdatedMessage:
    return TaskUpdatedMessage(
        subtype="task_updated",
        data={},
        task_id=task_id,
        patch={"status": status},
        status=None if in_patch else status,  # type: ignore[arg-type]
    )


def test_terminal_task_updated_clears_a_task_without_a_notification() -> None:
    tasks = BackgroundTasks()
    tasks.feed(_started("a"))
    tasks.feed(_started("b"))
    tasks.feed(_started("c"))
    tasks.feed(_updated("a", "killed"))
    tasks.feed(_updated("b", "completed", in_patch=True))
    tasks.feed(_updated("c", "running"))
    assert tasks.live() == [("c", "sleep 40")]
    tasks.feed(
        TaskNotificationMessage(
            subtype="task_notification",
            data={},
            task_id="c",
            status="failed",
            output_file="/tmp/o",
            summary="",
            uuid="n",
            session_id="s",
        )
    )
    assert tasks.live() == []


def test_nudge_names_the_work_and_says_the_job_ends() -> None:
    text = nudge_text(["task t1 (full pytest)", "pid 42 (pytest -q)"])
    assert "t1" in text and "pid 42" in text
    assert "killed" in text and "foreground" in text


def test_orphans_outside_a_bgjob_unit_report_nothing(tmp_path: Path) -> None:
    """agentjob run from a login shell: its session scope is full of
    unrelated processes; they are not the job's."""
    me = os.getpid()
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    scope = "user.slice/user-1000.slice/session-3.scope"
    (proc / str(me)).mkdir(parents=True)
    (proc / str(me) / "cgroup").write_text(f"0::/{scope}\n")
    (cg / scope).mkdir(parents=True)
    (cg / scope / "cgroup.procs").write_text(f"{me}\n999\n")
    assert orphans(proc_root=proc, cgroup_root=cg) == []


def test_orphans_without_cgroup_info_report_nothing(tmp_path: Path) -> None:
    assert orphans(proc_root=tmp_path / "proc", cgroup_root=tmp_path / "cg") == []


async def test_a_nudged_job_records_one_call_with_summed_usage(monkeypatch) -> None:
    """Two turns = one model_calls row: tokens summed per turn, cost is the
    CLI's running session total (the last result's), not added twice."""

    def result(text: str, cost: float) -> ResultMessage:
        return ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
            total_cost_usd=cost,
            usage={"input_tokens": 10, "output_tokens": 5},
            result=text,
        )

    turns = [[_started("t"), result("waiting", 0.1)], [result("done", 0.25)]]

    class FakeClient:
        def __init__(self, *_: object) -> None:
            self._turn: list = []

        async def connect(self) -> None: ...
        async def disconnect(self) -> None: ...

        async def query(self, prompt: str) -> None:
            self._turn = turns.pop(0)

        async def receive_response(self):  # type: ignore[no-untyped-def]
            for m in self._turn:
                yield m

    monkeypatch.setattr(job_mod, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(job_mod, "job_options", lambda *_: None)
    monkeypatch.setattr(job_mod, "orphans", lambda *a, **k: [])
    monkeypatch.delenv("BGJOB_MAX_NUDGES", raising=False)
    link = BotLink("http://127.0.0.1:1", "s")
    posted: list = []
    link.post = lambda path, body, timeout: posted.append((path, body)) or {}  # type: ignore[method-assign]
    spec = job_mod.JobSpec(
        purpose="background_job",
        name="j",
        prompt="p",
        model="sonnet",
        working_directory=Path("/tmp"),
        full_access=False,
        unrestricted=False,
    )
    # The task never ends; two turns are scripted, so one nudge.
    monkeypatch.setenv("BGJOB_MAX_NUDGES", "1")

    assert await job_mod.run_job(spec, link, out=io.StringIO()) == 3
    (row,) = [b for p, b in posted if p == "/internal/model_calls"]
    assert row["input_tokens"] == 20 and row["output_tokens"] == 10
    assert row["cost_usd"] == 0.25
    assert row["outcome"] == "incomplete"
