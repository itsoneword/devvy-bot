"""Every bot screen the UI gate builds (dv-4cd8).

Each ``ScreenSpec.build()`` produces its screen(s) offline, with fixed
state: the Codex catalog is the four models of ``test_model_keyboard.py``,
the Codex login is a temp ``HOME`` with or without the file /model reads
as "logged in", and the admin-menu builders get fixed values.
``tests/unit/test_screens_gate.py`` runs ``check()`` on every one;
``make test-core`` runs that test.

Add or change a bot screen → add or update its entry here. A screen that
fails today is registered with ``known_bad="<ticket id>"`` of the ticket that
fixes it; that is strict, so the fixing build has to drop the marker.

Not registered yet: /menu → Модели and Лимиты (they read the live
models.yaml and quota state; they need fakes first).
"""

import contextlib
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional, Tuple
from unittest import mock

from .capture import capture
from .model import Screen

Builder = Callable[[], Awaitable[List[Screen]]]

MODEL_GOAL = "pick the model this chat runs on (Claude or Codex)"
# The file under ~/.codex whose existence /model reads as "Codex logged in".
_CODEX_LOGIN_FILE = "auth" + ".json"


@dataclass(frozen=True)
class ScreenSpec:
    id: str
    goal: str
    build: Builder
    known_bad: Optional[str] = None


# --- fixed state ----------------------------------------------------------


def _codex_models() -> List[Any]:
    from ...agent_backends.codex_catalog import CodexModel

    return [
        CodexModel("gpt-5.6-sol", "GPT-5.6-Sol", 272000, 4),
        CodexModel("gpt-5.6-terra", "GPT-5.6-Terra", 272000, 7),
        CodexModel("gpt-5.6-luna", "GPT-5.6-Luna", 272000, 8),
        CodexModel("gpt-5.5", "GPT-5.5", 272000, 12),
    ]


@contextlib.contextmanager
def _codex_state(logged_in: bool) -> Iterator[None]:
    """A fixed Codex catalog and a temp HOME with or without the login file."""
    from ...agent_backends import codex_catalog

    models = _codex_models()

    async def get(force: bool = False) -> List[Any]:
        return list(models)

    catalog = codex_catalog.CODEX_CATALOG
    with tempfile.TemporaryDirectory() as home:
        if logged_in:
            (Path(home) / ".codex").mkdir()
            (Path(home) / ".codex" / _CODEX_LOGIN_FILE).write_text("{}")
        with (
            mock.patch.dict(os.environ, {"HOME": home}),
            mock.patch.object(catalog, "get", get),
            mock.patch.object(catalog, "_models", list(models), create=True),
        ):
            yield


def _orchestrator() -> Any:
    from ..orchestrator import MessageOrchestrator

    # Only the fields /model reads, like the handler tests.
    settings: Any = SimpleNamespace(
        agentic_mode=True,
        agent_backend="claude",
        agent_model="claude",
        claude_model="sonnet",
    )
    return MessageOrchestrator(settings, {})


# --- builders -------------------------------------------------------------


def _model(screen_id: str, user_data: Dict[str, Any], logged_in: bool) -> Builder:
    """/model through the real handler."""

    async def build() -> List[Screen]:
        with _codex_state(logged_in):
            orch = _orchestrator()
            context = SimpleNamespace(user_data=dict(user_data), bot_data={})
            return await capture(
                orch.agentic_model,
                context=context,
                screen_id=screen_id,
                goal=MODEL_GOAL,
            )

    return build


def _pure(
    screen_id: str,
    goal: str,
    make: Callable[[], Tuple[str, Any]],
    parse_mode: Optional[str] = None,
) -> Builder:
    """A ``(text, markup)`` builder, timed like a handler."""

    async def build() -> List[Screen]:
        # Import cost is not render time: the first screen would pay ~3 s for
        # admin_menu's imports and trip the `slow` gate.
        from ..orchestration import admin_menu  # noqa: F401

        start = time.perf_counter()
        text, markup = make()
        ms = (time.perf_counter() - start) * 1000
        return [Screen.from_markup(screen_id, text, markup, parse_mode, ms, goal)]

    return build


def _menu_root() -> Tuple[str, Any]:
    from ..orchestration.admin_menu import build_root

    return build_root()


def _menu_spend() -> Tuple[str, Any]:
    from ..orchestration.admin_menu import build_spend

    return build_spend(
        "Claude: 5ч 42% (сброс 14:00), неделя 18%\n\n"
        "Платные вызовы (сегодня / 7 дней):\n"
        "• openrouter: $0.25 / $1.75"
    )


def _menu_jobs_switch() -> Tuple[str, Any]:
    from ..orchestration.admin_menu import AdminMenuMixin

    return AdminMenuMixin._jobs_screen(None)


def _menu_settings() -> Tuple[str, Any]:
    from ...config import pump_unit
    from ..orchestration.admin_menu import build_settings

    # The live shape: parallel set by hand, the launch cap at its default,
    # one change to undo (the longest ↩ label).
    values: Dict[str, Optional[int]] = {name: None for name in pump_unit.EDITABLE}
    values[pump_unit.PUMP] = 5
    undo = {"name": pump_unit.STALL_CAP, "old": 12, "new": 13}
    return build_settings(
        Path("~/.config/systemd/user/taskpump.service"), values, None, undo
    )


def _pump_rows() -> list:
    return [
        {"id": "dv-0001", "title": "Short task", "pump": "building",
         "group": "running", "why": "build · phase 1/2 · 7 min", "hours": 0.1},
        {"id": "dv-0002", "title": "Questions for the owner", "pump": "asked",
         "group": "needs", "icon": "❓", "why": "answer 1, 2", "act": ""},
        {"id": "dv-0003", "title": "Held by a dependency", "pump": "",
         "group": "waiting", "icon": "⏸", "why": "waits dv-0001"},
        {"id": "dv-0004", "title": "A task with a longer title", "pump": "",
         "group": "queued", "why": "next: analysis"},
    ]  # fmt: skip


def _menu_pump() -> Tuple[str, Any]:
    from ..orchestration.admin_menu import build_pump

    return build_pump(_pump_rows(), None, 5)


def _menu_task_card() -> Tuple[str, Any]:
    from ..orchestration.admin_menu import build_task_card

    return build_task_card(_pump_rows()[1], "dv-0002")


def _menu_model_choices() -> Tuple[str, Any]:
    from ..orchestration.admin_menu import build_model_choices

    return build_model_choices(
        "pump_review", "claude", "sonnet", ("sonnet", "opus", "fable")
    )


def _menu_urgent_ask() -> Tuple[str, Any]:
    from ..orchestration.admin_menu import build_urgent_ask

    return build_urgent_ask("dv-0001")


def _spec(
    screen_id: str,
    goal: str,
    make: Callable[[], Tuple[str, Any]],
    known_bad: Optional[str] = None,
) -> ScreenSpec:
    return ScreenSpec(screen_id, goal, _pure(screen_id, goal, make), known_bad)


_CODEX_PICK = {
    "agent_backend": "openai_codex",
    "agent_model": "openai-codex/gpt-5.6-terra",
}

SCREENS: Dict[str, ScreenSpec] = {
    s.id: s
    for s in (
        ScreenSpec(
            "model.claude.logged_out",
            MODEL_GOAL,
            _model("model.claude.logged_out", {}, logged_in=False),
        ),
        ScreenSpec(
            "model.claude.logged_in",
            MODEL_GOAL,
            _model("model.claude.logged_in", {}, logged_in=True),
        ),
        ScreenSpec(
            "model.codex.logged_in",
            MODEL_GOAL,
            _model("model.codex.logged_in", _CODEX_PICK, logged_in=True),
        ),
        _spec(
            "menu.root",
            "open one of the owner's admin screens",
            _menu_root,
        ),
        _spec(
            "menu.spend",
            "see what Claude and the paid providers cost",
            _menu_spend,
        ),
        _spec(
            "menu.jobs_switch",
            "move all background jobs to another provider",
            _menu_jobs_switch,
        ),
        _spec(
            "menu.settings",
            "change the Pump caps",
            _menu_settings,
        ),
        _spec(
            "menu.pump",
            "see what the Pump is doing and push one task first",
            _menu_pump,
        ),
        _spec(
            "menu.task",
            "answer, push or retry one Pump task",
            _menu_task_card,
        ),
        _spec(
            "menu.model_choices",
            "pick the model a Pump stage runs on",
            _menu_model_choices,
        ),
        _spec(
            "menu.urgent_ask",
            "confirm or cancel an urgent run",
            _menu_urgent_ask,
        ),
    )
}
