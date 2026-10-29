"""dv-d63d phase 3: Codex as a job harness.

Contract pinned here (docs/research/dv-d63d/analysis.md, "Codex jobs" and
Phase 3):

* ``run_job(spec)`` with ``spec.route`` on ``openai_codex`` runs
  ``codex exec --json -C <cwd> -m <model sans prefix> <sandbox args>
  ... -`` (the ``codex`` on PATH), prompt on stdin, and prints its answer.
* Policy: the job mounts ``src/agent_bridge/codex_policy_hook.py`` unchanged
  through ``-c hooks.PreToolUse=[...]`` and gives the codex child
  ``DEVVY_POLICY_URL`` + ``AGENT_BRIDGE_TOKEN`` of a job-local endpoint that
  answers with ``policy.decide`` and asks the owner through ``BotLink.ask``.
* The ``model_calls`` row: harness ``openai_codex`` (provider ``openai``),
  tokens from ``turn.completed.usage``, session id = the codex thread id.
* The repo CLAUDE.md reaches the Codex job (Codex reads AGENTS.md):
  ``project_doc_fallback_filenames`` or, failing that, the prompt says so.
* Nudge (dv-b6dd): work left running after the answer -> a follow-up turn
  ``codex exec ... resume <thread_id> -`` with the nudge text.

No real codex: a fake ``codex`` script on PATH emits JSONL, runs the mounted
hook exactly as codex does (call JSON on stdin, deny JSON on stdout) and
logs every invocation.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from src.model_router.job import INCOMPLETE_EXIT, BotLink, JobSpec, run_job

# The bot app / DB fixtures of the Claude-job tests: same wire, same tables.
from tests.unit.test_model_router_job import (  # noqa: F401  (fixture)
    _AppLink,
    _bot_app,
    _rows,
    bot_db,
)

CODEX_JOBS_YAML = """
jobs:
  background_job:
    harness: openai_codex
  pump_implement:
    harness: openai_codex
"""
CODEX_MODEL = "gpt-6-sol"
THREAD = "th-job-1"

FAKE_CODEX = r"""#!{python}
import json, os, re, subprocess, sys

LOG = {log!r}
PLAN = {plan!r}

argv = sys.argv[1:]
if not argv or argv[0] != "exec":
    print("codex-cli 0.200.0-fake")
    sys.exit(0)

with open(PLAN) as fh:
    plan = json.load(fh)
try:
    with open(LOG) as fh:
        n = sum(1 for _ in fh)
except OSError:
    n = 0
turn = plan[min(n, len(plan) - 1)]

prompt = sys.stdin.read() if argv and argv[-1] == "-" else ""
entry = {{
    "argv": argv,
    "stdin": prompt,
    "cwd": os.getcwd(),
    "has_policy_url": bool(os.environ.get("DEVVY_POLICY_URL")),
    "has_token": bool(os.environ.get("AGENT_BRIDGE_TOKEN")),
    "tools": [],
}}


def emit(ev):
    print(json.dumps(ev), flush=True)


def hook_command():
    for a in argv:
        if a.startswith("hooks.PreToolUse="):
            m = re.search(r'command=("(?:[^"\\]|\\.)*")', a)
            if m:
                return json.loads(m.group(1))
    return None


thread = THREAD_ID = {thread!r}
if "resume" in argv:
    thread = argv[argv.index("resume") + 1]
emit({{"type": "thread.started", "thread_id": thread}})
emit({{"type": "turn.started"}})

for i, command in enumerate(turn.get("commands", [])):
    item_id = "item_%d" % i
    emit({{"type": "item.started", "item": {{"id": item_id, "type": "command_execution",
          "command": command, "status": "in_progress"}}}})
    cmd = hook_command()
    decision = "no-hook"
    reason = ""
    if cmd:
        payload = json.dumps({{
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {{"command": command}},
            "tool_use_id": "call_%d" % i,
            "cwd": os.getcwd(),
        }})
        try:
            res = subprocess.run(cmd, shell=True, input=payload, text=True,
                                 capture_output=True, timeout=60, env=os.environ)
            out = res.stdout.strip()
            decision = "allow"
            if out:
                spec = json.loads(out).get("hookSpecificOutput", {{}})
                if spec.get("permissionDecision") == "deny":
                    decision = "deny"
                    reason = spec.get("permissionDecisionReason", "")
        except subprocess.TimeoutExpired:
            decision = "hook-timeout"
    ran = decision == "allow"
    entry["tools"].append({{"command": command, "decision": decision,
                            "reason": reason, "ran": ran}})
    if ran:
        emit({{"type": "item.completed", "item": {{"id": item_id,
              "type": "command_execution", "command": command,
              "aggregated_output": "done\n", "exit_code": 0,
              "status": "completed"}}}})
    else:
        emit({{"type": "item.completed", "item": {{"id": item_id,
              "type": "command_execution", "command": command,
              "aggregated_output": "", "status": "declined"}}}})

answer = turn.get("answer", "ok")
if "-o" in argv:
    with open(argv[argv.index("-o") + 1], "w") as fh:
        fh.write(answer)
emit({{"type": "item.completed", "item": {{"id": "msg", "type": "agent_message",
      "text": answer}}}})
emit({{"type": "turn.completed", "usage": turn.get("usage",
      {{"input_tokens": 1200, "cached_input_tokens": 200, "output_tokens": 80}})}})

with open(LOG, "a") as fh:
    fh.write(json.dumps(entry) + "\n")
sys.exit(int(turn.get("exit", 0)))
"""


class FakeCodex:
    def __init__(self, root: Path) -> None:
        self.bin = root / "fakebin"
        self.bin.mkdir()
        self.log = root / "codex-log.jsonl"
        self.plan = root / "codex-plan.json"
        script = self.bin / "codex"
        script.write_text(
            FAKE_CODEX.format(
                python=sys.executable,
                log=str(self.log),
                plan=str(self.plan),
                thread=THREAD,
            ),
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        self.set_plan([{"answer": "ok"}])

    def set_plan(self, turns: List[Dict[str, Any]]) -> None:
        self.plan.write_text(json.dumps(turns), encoding="utf-8")

    def calls(self) -> List[Dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(ln) for ln in self.log.read_text().splitlines() if ln]


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCodex:
    fake = FakeCodex(tmp_path)
    monkeypatch.setenv("PATH", f"{fake.bin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("BGJOB_MAX_NUDGES", raising=False)
    _patch_orphans(monkeypatch, lambda *a, **k: [])
    return fake


def _patch_orphans(monkeypatch: pytest.MonkeyPatch, fn: Any) -> None:
    """The orphan scan, wherever the Codex job reads it from."""
    monkeypatch.setattr("src.model_router.job.orphans", fn)
    try:
        import src.model_router.job_codex as job_codex  # type: ignore
    except ImportError:
        return
    if hasattr(job_codex, "orphans"):
        monkeypatch.setattr(job_codex, "orphans", fn)


def _codex_route(tmp_path: Path, name: str = "background_job") -> Any:
    from src.model_router.routes import load_call_routes

    path = tmp_path / "models.yaml"
    path.write_text(CODEX_JOBS_YAML, encoding="utf-8")
    return load_call_routes(path).resolve_job(name)


def _codex_spec(
    cwd: Path,
    route: Any,
    *,
    name: str = "codex-job",
    unrestricted: bool = True,
    full_access: bool = True,
    prompt: str = "say ok",
    **kw: Any,
) -> JobSpec:
    return JobSpec(
        purpose="background_job",
        name=name,
        prompt=prompt,
        model=route.model,
        route=route,
        working_directory=cwd,
        full_access=full_access,
        unrestricted=unrestricted,
        profile="devops",
        **kw,
    )


async def _run(spec: JobSpec, link: BotLink, out: Optional[io.StringIO] = None) -> int:
    return await asyncio.wait_for(
        run_job(spec, link, out=out if out is not None else io.StringIO()), 120
    )


def _hook_arg(argv: List[str]) -> str:
    hooks = [a for a in argv if a.startswith("hooks.PreToolUse=")]
    assert hooks, f"no PreToolUse hook mounted: {argv}"
    return hooks[0]


# -- jobs.background_job on openai_codex: bgjob answers, row harness codex --


def test_background_job_row_on_codex_resolves_to_the_codex_harness(
    tmp_path: Path,
) -> None:
    """Guard (phase 2): the table row is what puts the job on Codex."""
    route = _codex_route(tmp_path)
    assert route.harness == "openai_codex"
    assert route.provider == "openai"
    assert (route.model or "").endswith(CODEX_MODEL)


async def test_background_job_on_codex_answers_and_row_is_openai_codex(
    tmp_path: Path, fake_codex: FakeCodex, bot_db: Any
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    fake_codex.set_plan([{"answer": "ok from codex", "commands": ["ls -la"]}])
    link = _AppLink(_bot_app(bot_db))
    out = io.StringIO()

    code = await _run(_codex_spec(work, _codex_route(tmp_path)), link, out)

    assert code == 0
    assert "ok from codex" in out.getvalue()
    (call,) = fake_codex.calls()
    argv = call["argv"]
    assert argv[0] == "exec" and "--json" in argv
    assert argv[argv.index("-C") + 1] == str(work)
    # The Codex CLI takes the bare model id, not the router's prefixed name.
    assert argv[argv.index("-m") + 1] == CODEX_MODEL
    assert call["stdin"].strip().endswith("say ok") or "say ok" in call["stdin"]

    (row,) = await _rows(bot_db, "model_calls")
    assert row["harness"] == "openai_codex"
    assert row["provider"] == "openai"
    assert (row["model"] or "").endswith(CODEX_MODEL)
    assert row["outcome"] == "ok"
    assert row["output_tokens"] == 80
    assert row["input_tokens"] in (1000, 1200)
    assert row["session_id"] == THREAD
    assert row["job_name"] == "codex-job"
    # The shell call the job made is traced under the same row.
    tools = await _rows(bot_db, "tool_calls")
    assert [t["tool"] for t in tools] == ["Bash"]
    assert {t["model_call_id"] for t in tools} == {row["id"]}


async def test_codex_job_that_fails_exits_nonzero_and_records_an_error_row(
    tmp_path: Path, fake_codex: FakeCodex, bot_db: Any
) -> None:
    """added: a codex exit != 0 is a failed job on Codex (no fallback, owner
    answer 2026-10-26): exit != 0, one codex run, an error row on openai_codex."""
    work = tmp_path / "work"
    work.mkdir()
    fake_codex.set_plan([{"answer": "", "exit": 1}])
    link = _AppLink(_bot_app(bot_db))

    code = await _run(_codex_spec(work, _codex_route(tmp_path)), link)

    assert code != 0
    assert len(fake_codex.calls()) == 1
    (row,) = await _rows(bot_db, "model_calls")
    assert row["harness"] == "openai_codex"
    assert row["outcome"] == "error"


# -- A destructive command in a Codex job asks the owner; deny blocks it --


@pytest.mark.parametrize("approved", [False, True])
async def test_destructive_command_in_a_codex_job_asks_the_owner(
    tmp_path: Path, fake_codex: FakeCodex, bot_db: Any, approved: bool
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    fake_codex.set_plan([{"answer": "pushed?", "commands": ["git push origin main"]}])
    link = _AppLink(_bot_app(bot_db))
    link.ask = AsyncMock(return_value=approved)  # type: ignore[method-assign]

    code = await _run(
        _codex_spec(work, _codex_route(tmp_path), name="pump-dv-x-implement"), link
    )

    assert code == 0
    (call,) = fake_codex.calls()
    # The bot's own hook script is mounted, and the child can reach the gate.
    assert "codex_policy_hook.py" in _hook_arg(call["argv"])
    assert call["has_policy_url"] and call["has_token"]
    # One ask to the owner, through the same BotLink as Claude jobs.
    link.ask.assert_awaited_once()
    reason, preview = link.ask.await_args.args[:2]
    assert "git push" in preview
    source = (
        link.ask.await_args.args[2]
        if len(link.ask.await_args.args) > 2
        else link.ask.await_args.kwargs.get("source")
    )
    assert source == "pump-dv-x-implement"
    (tool,) = call["tools"]
    if approved:
        assert tool["decision"] == "allow" and tool["ran"]
    else:
        assert tool["decision"] == "deny" and not tool["ran"]
        assert "Denied by owner" in tool["reason"]


async def test_hard_deny_in_a_codex_job_is_blocked_without_asking(
    tmp_path: Path, fake_codex: FakeCodex, bot_db: Any
) -> None:
    """added: a deny rule (credential file) blocks the call and never reaches
    the owner -- same decide() as the Claude job, no rule of its own."""
    work = tmp_path / "work"
    work.mkdir()
    fake_codex.set_plan([{"answer": "x", "commands": ["cat .env", "ls -la"]}])
    link = _AppLink(_bot_app(bot_db))
    link.ask = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await _run(_codex_spec(work, _codex_route(tmp_path)), link)

    (call,) = fake_codex.calls()
    denied, allowed = call["tools"]
    assert denied["decision"] == "deny" and not denied["ran"]
    assert allowed["decision"] == "allow" and allowed["ran"]
    link.ask.assert_not_awaited()


# -- Codex pump stage reads the repo CLAUDE.md, writes only in its worktree --


async def test_codex_pump_stage_gets_claude_md_and_runs_in_its_worktree(
    tmp_path: Path, fake_codex: FakeCodex, bot_db: Any
) -> None:
    worktree = tmp_path / "pump-worktrees" / "dv-x"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /x/.git/worktrees/dv-x\n")
    (worktree / "CLAUDE.md").write_text("# project rules\n")
    link = _AppLink(_bot_app(bot_db))

    code = await _run(
        _codex_spec(
            worktree,
            _codex_route(tmp_path, "pump_implement"),
            name="pump-dv-x-implement",
            setting_sources=("project",),
            free_root=worktree,
            prompt="implement phase 3",
        ),
        link,
    )

    assert code == 0
    (call,) = fake_codex.calls()
    argv = call["argv"]
    # Codex reads AGENTS.md; the job points it at CLAUDE.md instead.
    doc_args = [a for a in argv if "project_doc_fallback_filenames" in a]
    assert (doc_args and "CLAUDE.md" in doc_args[0]) or "CLAUDE.md" in call["stdin"]
    # It runs in the worktree, and nothing in the argv points it elsewhere.
    assert argv[argv.index("-C") + 1] == str(worktree)
    assert call["cwd"] == str(worktree) or argv[argv.index("-C") + 1] == str(worktree)
    assert "--add-dir" not in argv
    assert "implement phase 3" in call["stdin"]


async def test_sandboxed_codex_job_gets_the_confined_profile_not_full_access(
    tmp_path: Path, fake_codex: FakeCodex, bot_db: Any
) -> None:
    """The codex sandbox follows the profile, as in chat
    (``codex_sandbox.sandbox_args``): devops (unrestricted) gets
    danger-full-access inside the bgjob box, a sandboxed profile the
    confined permission profile on its own cwd."""
    work = tmp_path / "work"
    work.mkdir()
    link = _AppLink(_bot_app(bot_db))

    await _run(
        _codex_spec(
            work, _codex_route(tmp_path), unrestricted=False, full_access=False
        ),
        link,
    )
    await _run(_codex_spec(work, _codex_route(tmp_path)), link)

    confined, unrestricted = (c["argv"] for c in fake_codex.calls())
    assert "danger-full-access" not in " ".join(confined)
    assert any(a.startswith("default_permissions=") for a in confined)
    joined = " ".join(confined)
    assert str(work) in joined  # the confined profile writes its own cwd
    i = unrestricted.index("-s")
    assert unrestricted[i + 1] == "danger-full-access"


# -- Work left running gets a nudge via codex resume, as on Claude jobs --


async def test_work_left_running_gets_a_nudge_via_codex_resume(
    tmp_path: Path,
    fake_codex: FakeCodex,
    bot_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    fake_codex.set_plan(
        [
            {"answer": "started the build in the background"},
            {"answer": "build finished, all green"},
        ]
    )
    scans = iter([[(4242, "nohup make all")], []])
    _patch_orphans(monkeypatch, lambda *a, **k: next(scans, []))
    link = _AppLink(_bot_app(bot_db))
    out = io.StringIO()

    code = await _run(_codex_spec(work, _codex_route(tmp_path)), link, out)

    assert code == 0
    first, nudge = fake_codex.calls()
    assert "resume" not in first["argv"]
    argv = nudge["argv"]
    assert argv[0] == "exec" and "resume" in argv
    assert argv[argv.index("resume") + 1] == THREAD
    assert argv[-1] == "-"
    # The nudge text of the Claude job, naming the live work.
    assert "still running" in nudge["stdin"] and "nohup make all" in nudge["stdin"]
    # The resumed turn keeps the gate.
    assert "codex_policy_hook.py" in _hook_arg(argv)
    # The finished answer wins; one row for the whole job.
    assert "build finished, all green" in out.getvalue()
    (row,) = await _rows(bot_db, "model_calls")
    assert row["harness"] == "openai_codex" and row["outcome"] == "ok"


async def test_codex_work_still_live_after_the_nudges_exits_incomplete(
    tmp_path: Path,
    fake_codex: FakeCodex,
    bot_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """added: as on Claude jobs -- two nudges max, then exit 3 and an
    ``incomplete`` row."""
    work = tmp_path / "work"
    work.mkdir()
    fake_codex.set_plan([{"answer": "still waiting"}])
    _patch_orphans(monkeypatch, lambda *a, **k: [(4242, "nohup make all")])
    link = _AppLink(_bot_app(bot_db))

    code = await _run(_codex_spec(work, _codex_route(tmp_path)), link)

    assert code == INCOMPLETE_EXIT
    calls = fake_codex.calls()
    assert len(calls) == 3  # the turn + two nudges
    assert all("resume" in c["argv"] for c in calls[1:])
    (row,) = await _rows(bot_db, "model_calls")
    assert row["outcome"] == "incomplete"
