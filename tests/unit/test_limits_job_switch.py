"""The owner's job switch (dv-d63d phase 4; moved from /limits into Модели,
dv-f6af phase 2: ``menu:jobs:<target>``, the old ``limits:jobs:*`` buttons of
earlier messages still work and share one handler).

Contract (docs/research/dv-d63d/analysis.md, "Owner switch" and Phase 4):

* ``/limits`` carries one keyboard row ``Jobs: Claude · Codex · OpenRouter ·
  Reset`` whose callback data is ``limits:jobs:<claude|openai_codex|openrouter|
  reset>``, and a jobs line. Both render even when no Codex backend is
  connected (today the command returns early with "Codex backend не
  подключён").
* A tap is owner-only (a callback bypasses the command gate, like ``vset:``).
  It writes ``{"job_route": {"harness": ..., "set_at", "by"}}`` into the
  dv-7ad9 record ``DataLayout.model_quota`` through
  ``src.model_router.quota.set_job_route`` and rewrites only that key; Reset
  removes it. ``quota.job_route_override()`` reads it back.
* ``CallRoutes.resolve_job`` (read per job process by agentjob) honours the
  key above the table and above an explicit ``--model``, so the next Pump
  stage moves with no restart and no agent turn. A garbled value is ignored.
* Nothing but the /limits callback ever sets the key (owner answer: no
  automatic switch on a quota error).

Buttons are found by their callback data and pressed through the
CallbackQueryHandler the orchestrator really registers, so a ``limits:``
prefix missing from the handler pattern fails here. All state lives under a
tmp ``DEVVY_DATA_ROOT``; HOME is a tmp dir with a Codex login.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import CallbackQueryHandler

from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config
from src.model_router.routes import load_call_routes, process_call_routes
from src.model_router.types import JOB_ROUTE_NAMES

REPO = Path(__file__).resolve().parents[2]
USER_ID = 777
JOBS_PREFIX = "limits:jobs:"

# A limit record as dv-7ad9 writes it into the same file: must survive taps.
LIMIT_RECORD = {
    "kind": "weekly limit",
    "until": "2026-10-27T09:00:00Z",
    "message": "You've hit your weekly limit · resets 9am (UTC)",
    "source": "bgjob:test",
    "recorded": "2026-10-26T08:13:00Z",
}


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    """Data root, HOME (with a Codex login) and an empty route table in tmp."""
    data = tmp_path / "data"
    data.mkdir()
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"tokens": {}}', encoding="utf-8")
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(data))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MODEL_ROUTES_PATH", str(tmp_path / "no-models.yaml"))
    monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)
    for var in (
        "PUMP_ANALYZE_MODEL",
        "PUMP_IMPLEMENT_MODEL",
        "PUMP_TESTDESIGN_MODEL",
        "PUMP_REVIEW_MODEL",
        "PUMP_ARCHITECT_MODEL",
        "PUMP_ACCEPTANCE_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    return data


def _quota_path() -> Path:
    from src.utils.paths import data_layout

    return data_layout().model_quota


def _record() -> dict:
    path = _quota_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _codex_snapshot() -> dict:
    return {
        "rate_limits": {
            "rateLimitsByLimitId": {
                "codex": {"planType": "plus", "primary": {"usedPercent": 12}}
            }
        },
        "usage": {"summary": {"lifetimeTokens": 123}},
    }


def _backend(backend_id: str) -> MagicMock:
    b = MagicMock(
        spec=(
            "backend_id",
            "default_model",
            "run_command",
            "resumable_session_for",
            "sessions_for_user",
            "attach_tools",
            "read_account_limits",
        )
    )
    b.backend_id = backend_id
    b.run_command = AsyncMock(side_effect=AssertionError("no model turn"))
    b.resumable_session_for = AsyncMock(return_value=None)
    b.sessions_for_user = AsyncMock(return_value=[])
    return b


def _backends(with_codex: bool = True) -> dict:
    claude = _backend("claude")
    claude.read_account_limits = AsyncMock(side_effect=AssertionError("no LLM"))
    out = {"claude": claude}
    if with_codex:
        codex = _backend("openai_codex")
        codex.read_account_limits = AsyncMock(return_value=_codex_snapshot())
        out["openai_codex"] = codex
    return out


def _settings(workdir: Path):
    return create_test_config(approved_directory=str(workdir))


def _context(settings, bot_data: dict, role: str = "owner") -> MagicMock:
    context = MagicMock()
    context.user_data = {
        "role": role,
        "agent_backend": "claude",
        "_display_name_checked": True,
        "_prefs_loaded": True,
    }
    auth = MagicMock()
    auth.get_user_role = MagicMock(return_value=role)
    context.bot_data = {
        "settings": settings,
        "storage": None,
        "rate_limiter": None,
        "audit_logger": None,
        "window_repo": None,
        "evaluator_service": None,
        "profile_registry": None,
        "auth_manager": auth,
        **bot_data,
    }
    return context


def _command_update() -> MagicMock:
    update = MagicMock()
    update.effective_user.id = USER_ID
    update.message.text = "/limits"
    update.message.message_id = 1
    update.message.message_thread_id = None
    update.message.chat.id = 999
    update.message.chat.type = "private"
    update.message.reply_text = AsyncMock(return_value=MagicMock())
    update.effective_message = update.message
    return update


def _sent(update: MagicMock) -> Tuple[str, List[Any]]:
    """All text /limits replied with, and every inline button it offered."""
    texts, buttons = [], []
    for call in update.message.reply_text.call_args_list:
        text = call.args[0] if call.args else call.kwargs.get("text", "")
        texts.append(str(text))
        markup = call.kwargs.get("reply_markup")
        for row in getattr(markup, "inline_keyboard", None) or []:
            buttons.extend(row)
    return "\n".join(texts), buttons


async def _limits(orch, context) -> Tuple[str, List[Any]]:
    update = _command_update()
    await orch.agentic_limits(update, context)
    return _sent(update)


def _jobs_button(buttons: List[Any], target: str) -> Any:
    data = f"{JOBS_PREFIX}{target}"
    found = [b for b in buttons if getattr(b, "callback_data", None) == data]
    assert found, (data, [(b.text, b.callback_data) for b in buttons])
    return found[0]


async def _press(
    orch: MessageOrchestrator, context: MagicMock, data: str, user_id: int = USER_ID
) -> MagicMock:
    """Press a button through the CallbackQueryHandler the bot registers."""
    app = MagicMock()
    app.add_handler = MagicMock()
    orch.register_handlers(app)
    handlers = [
        c.args[0]
        for c in app.add_handler.call_args_list
        if c.args and isinstance(c.args[0], CallbackQueryHandler)
    ]
    matching = [h for h in handlers if h.pattern is None or h.pattern.match(data)]
    assert matching, f"no registered callback handler takes {data!r}"
    query = MagicMock()
    query.data = data
    query.from_user.id = user_id
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message.reply_text = AsyncMock()
    query.message.chat.id = 999
    query.message.message_thread_id = None
    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = user_id
    update.effective_chat.id = 999
    update.effective_message = query.message
    await matching[0].callback(update, context)
    return query


def _agentjob_route(route: str, *extra: str) -> dict:
    """What the next job of ``route`` resolves to: a fresh agentjob process,
    exactly as bgjob probes it before every job (no bot restart involved)."""
    import os

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ["HOME"],
        "DEVVY_DATA_ROOT": os.environ["DEVVY_DATA_ROOT"],
        "MODEL_ROUTES_PATH": os.environ["MODEL_ROUTES_PATH"],
    }
    out = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "agentjob"),
            "--print-route",
            "--route",
            route,
            *extra,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --- scenario: owner taps Jobs: Codex -------------------------------------------


async def test_owner_taps_jobs_codex_and_the_next_pump_stage_runs_on_codex(tmp_path):
    """Scenario: owner taps Jobs: Codex in /limits → the next pump stage runs
    on Codex, no restart (same bot process, next agentjob reads the switch)."""
    # Before: the review stage is on the table's Claude row.
    assert _agentjob_route("pump_review")["harness"] == "claude"

    settings = _settings(tmp_path)
    backends = _backends()
    orch = MessageOrchestrator(settings, {})
    context = _context(settings, {"backends": backends})

    await _press(orch, context, "menu:jobs:openai_codex")

    # Stored as the dv-7ad9 record's job_route key.
    assert _record()["job_route"]["harness"] == "openai_codex"
    from src.model_router import quota

    assert quota.job_route_override() == "openai_codex"

    # The next pump stage (a fresh agentjob, as bgjob runs it) is on Codex.
    route = _agentjob_route("pump_review")
    assert route["harness"] == "openai_codex"
    assert route["provider"] == "openai"
    assert route["model"]  # the Codex model from the jobs table
    # Every job moves (monitor included), in this process too.
    routes = process_call_routes()
    for name in JOB_ROUTE_NAMES:
        assert routes.resolve_job(name).harness == "openai_codex", name

    # No model call anywhere on this path.
    for backend in backends.values():
        backend.run_command.assert_not_awaited()


async def test_tapping_codex_twice_over_a_limit_record_keeps_that_record(tmp_path):
    """Scenario 1, bad sequence / existing data: the file already holds a
    dv-7ad9 limit record and Codex is tapped twice. The limit keys survive,
    the switch is one key, still Codex."""
    path = _quota_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(LIMIT_RECORD), encoding="utf-8")

    settings = _settings(tmp_path)
    orch = MessageOrchestrator(settings, {})
    context = _context(settings, {"backends": _backends()})

    await _press(orch, context, f"{JOBS_PREFIX}openai_codex")
    await _press(orch, context, f"{JOBS_PREFIX}openai_codex")

    body = _record()
    for key, value in LIMIT_RECORD.items():
        assert body[key] == value, key
    assert body["job_route"]["harness"] == "openai_codex"
    assert _agentjob_route("pump_implement")["harness"] == "openai_codex"


# --- scenario: Reset ----------------------------------------------------------


async def test_reset_returns_all_jobs_to_the_models_yaml_table(tmp_path, monkeypatch):
    """Scenario: Reset returns all jobs to the models.yaml table."""
    table = tmp_path / "models.yaml"
    table.write_text(
        "jobs:\n"
        "  pump_review: {models: {claude: fable}}\n"
        "  pump_implement: {models: {claude: sonnet}}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_ROUTES_PATH", str(table))
    path = _quota_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(LIMIT_RECORD), encoding="utf-8")

    settings = _settings(tmp_path)
    orch = MessageOrchestrator(settings, {})
    context = _context(settings, {"backends": _backends()})

    await _press(orch, context, f"{JOBS_PREFIX}openai_codex")
    assert _agentjob_route("pump_review")["harness"] == "openai_codex"

    await _press(orch, context, f"{JOBS_PREFIX}reset")

    from src.model_router import quota

    assert quota.job_route_override() is None
    body = _record()
    assert "job_route" not in body
    for key, value in LIMIT_RECORD.items():  # the limit record is not touched
        assert body[key] == value, key

    expected = load_call_routes(table)
    live = process_call_routes()
    for name in JOB_ROUTE_NAMES:
        assert live.resolve_job(name) == expected.resolve_job(name), name
    assert _agentjob_route("pump_review") == {
        "harness": "claude",
        "provider": "anthropic_sub",
        "model": "fable",
    }
    assert _agentjob_route("pump_implement")["model"] == "sonnet"


# --- scenario: non-owner ------------------------------------------------------


@pytest.mark.parametrize("target", ["reset", "openrouter", "claude", "openai_codex"])
async def test_non_owner_tap_on_the_jobs_buttons_changes_nothing(tmp_path, target):
    """Scenario: a non-owner tap on the jobs buttons does nothing; the switch
    stays as it was (here: Codex, set by the owner)."""
    settings = _settings(tmp_path)
    orch = MessageOrchestrator(settings, {})
    owner = _context(settings, {"backends": _backends()})
    await _press(orch, owner, f"{JOBS_PREFIX}openai_codex")
    before = _quota_path().read_bytes()

    guest = _context(settings, {"backends": _backends()}, role="guest")
    query = await _press(orch, guest, f"{JOBS_PREFIX}{target}", user_id=4242)

    assert _quota_path().read_bytes() == before
    from src.model_router import quota

    assert quota.job_route_override() == "openai_codex"
    assert _agentjob_route("pump_review")["harness"] == "openai_codex"
    replies = " ".join(
        str(c.args[0] if c.args else c.kwargs.get("text", ""))
        for c in query.edit_message_text.call_args_list
        + query.answer.call_args_list
        + query.message.reply_text.call_args_list
    )
    assert re.search(r"owner", replies, re.IGNORECASE), replies


async def test_non_owner_tap_without_a_switch_writes_no_file(tmp_path):
    """Same scenario, empty case: no switch on, a guest taps Codex → still no
    switch, no record file created."""
    settings = _settings(tmp_path)
    orch = MessageOrchestrator(settings, {})
    guest = _context(settings, {"backends": _backends()}, role="guest")

    await _press(orch, guest, f"{JOBS_PREFIX}openai_codex", user_id=4242)

    assert not _quota_path().exists()
    assert _agentjob_route("pump_review")["harness"] == "claude"


# --- scenario: /limits shows the switch -----------------------------------------


async def test_models_top_line_shows_the_switched_harness_while_it_is_on(tmp_path):
    """Scenario: the Модели top line names the switched harness while the
    switch is on; /limits carries no jobs line or button at all."""
    from src.bot.orchestration.commands.info import jobs_switch_name

    settings = _settings(tmp_path)
    orch = MessageOrchestrator(settings, {})
    context = _context(settings, {"backends": _backends()})

    assert jobs_switch_name() == "по таблице"
    await _press(orch, context, "menu:jobs:openai_codex")
    assert jobs_switch_name() == "Codex"
    text, buttons = await _limits(orch, context)
    assert "job" not in text.lower() and not any(
        (b.callback_data or "").startswith(JOBS_PREFIX) for b in buttons
    ), text

    await _press(orch, context, "menu:jobs:table")
    assert jobs_switch_name() == "по таблице"


# --- scenario: the switch without a Codex backend -------------------------------


async def test_switch_works_without_a_codex_backend(tmp_path):
    """Scenario: the switch works even when the Codex backend is not
    connected (no backends["openai_codex"], no codex_integration)."""
    settings = _settings(tmp_path)
    orch = MessageOrchestrator(settings, {})
    context = _context(
        settings, {"backends": _backends(with_codex=False), "codex_integration": None}
    )

    await _press(orch, context, "menu:jobs:openai_codex")
    assert _agentjob_route("monitor")["harness"] == "openai_codex"


# --- added ----------------------------------------------------------------------


def test_switch_beats_an_explicit_claude_model(tmp_path):
    """added: under the Codex switch a job_start-style ``--model sonnet`` is
    dropped (logged), never sent to Codex."""
    from src.model_router import quota

    quota.set_job_route("openai_codex", "owner")
    route = _agentjob_route("background_job", "--model", "sonnet")
    assert route["harness"] == "openai_codex"
    assert route["model"] != "sonnet"
    assert process_call_routes().resolve_job(
        "background_job", model="sonnet"
    ).model not in (None, "sonnet")


@pytest.mark.parametrize(
    "body",
    [
        "{not json",
        json.dumps({"job_route": {"harness": "gemini"}}),
        json.dumps({"job_route": "openai_codex!"}),
        json.dumps(LIMIT_RECORD),
    ],
)
def test_garbled_or_absent_switch_leaves_jobs_on_the_table(body):
    """added: a garbled, unknown or absent job_route value is ignored; every
    job resolves exactly as the table says."""
    from src.model_router import quota

    path = _quota_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")

    assert quota.job_route_override() is None
    live = process_call_routes()
    table = load_call_routes(None)
    for name in JOB_ROUTE_NAMES:
        assert live.resolve_job(name) == table.resolve_job(name), name


def test_only_the_limits_callback_sets_the_job_switch():
    """added: owner answer 4.1 — never switch automatically on a quota error.
    The only caller of ``set_job_route`` in src/ and scripts/ is the bot's
    callback code."""
    pattern = re.compile(r"\bset_job_route\s*\(")
    callers = []
    for base in (REPO / "src", REPO / "scripts"):
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in ("", ".py"):
                continue
            if path == REPO / "src" / "model_router" / "quota.py":
                continue  # the definition and its own helpers
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("def ") or stripped.startswith("#"):
                    continue
                if pattern.search(line):
                    callers.append(path.relative_to(REPO).as_posix())
    assert callers, "set_job_route is never called (the /limits tap must call it)"
    assert set(callers) <= {
        "src/bot/orchestration/callbacks.py",
        "src/bot/orchestration/commands/info.py",
    }, callers
