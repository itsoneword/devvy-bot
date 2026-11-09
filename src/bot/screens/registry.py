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

Not registered yet: /menu → Лимиты (it reads the live quota state; it
needs fakes first). Модели gets its facts as arguments (dv-3881).
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
RESTART_GOAL = "see why /restart was refused and force it only if sure"
AGENTS_OFF_GOAL = "see which agent the boot switched off, and why"
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
        "Активный чат: Claude · модель: opus\n\n"
        "Claude: 5ч 42% (сброс 14:00), неделя 18%\n"
        "  использует: все стадии Pump и фоновые задачи\n\n"
        "Платные вызовы (сегодня / 7 дней):\n"
        "• openrouter: $0.25 / $1.75\n\n" + _BUDGET
    )


# The /usage block that ends /limits (dv-3881), as an owner sees it.
_BUDGET = (
    "Бюджет (= /usage):\n"
    "💸 Usage (owner)\n"
    "Today: $0.00\n"
    "Last 30 days: $1.75 (12 requests)\n"
    "Budget window (24h): $0.00 (no cap)\n"
    "Per-request cap: none"
)


def _menu_spend_codex_limit() -> Tuple[str, Any]:
    from ..orchestration.admin_menu import build_spend

    # dv-3881: jobs switched to Codex, Codex at its limit: the stages wait,
    # Pump is not frozen.
    return build_spend(
        "Активный чат: Claude · модель: opus\n\n"
        "Claude: 5ч 42% (сброс 14:00), неделя 18%\n\n"
        "Платных вызовов за 7 дней нет.\n\n"
        "Другой провайдер — отдельный лимит:\n"
        "⛔ Codex: лимит до 12:23 UTC\n"
        "  ждут сброса: все стадии Pump и фоновые задачи (переключатель)\n"
        "  Pump не заморожен: ждут только стадии на Codex\n"
        "Plan: plus · 5h: использовано 100%\n\n" + _BUDGET
    )


def _menu_jobs_switch() -> Tuple[str, Any]:
    from ..orchestration.admin_menu import AdminMenuMixin

    return AdminMenuMixin._jobs_screen(None)


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


def _pending_questions() -> Dict[str, Any]:
    return {
        "id": "dv-0002", "title": "Questions for the owner", "board": "devvy",
        "pump": "asked", "phase": "1/3", "kind": "asked", "block": "",
        "questions": [
            {"num": "1.1", "text": "One chat model for every agent?",
             "default": "yes", "decided": False},
            {"num": "1.2", "text": "Scheduled turns always on Claude: change?",
             "default": "only show it", "decided": False},
            {"num": "2.1", "text": "Auto-returns 0-5, tick 3-30 min?",
             "default": "yes", "decided": True},
        ],
    }  # fmt: skip


def _menu_pump() -> Tuple[str, Any]:
    from ..orchestration.admin_menu import build_pump

    return build_pump(_pump_rows(), None)


def _menu_pump_frozen_quota() -> Tuple[str, Any]:
    from ..orchestration.admin_menu import build_pump

    # dv-3881: frozen by the Claude limit, the head says until when; a
    # frozen Pump launches nothing, so nothing runs.
    frozen = {"by": "quota", "until": "2026-11-02T12:23:00Z", "reason": "limit"}
    rows = [t for t in _pump_rows() if t.get("group") != "running"]
    return build_pump(rows, frozen)


def _menu_task_card() -> Tuple[str, Any]:
    from ..orchestration.admin_menu import build_task_card

    return build_task_card(_pump_rows()[1], "dv-0002")


def _menu_task_asked() -> Tuple[str, Any]:
    from ..orchestration.admin_menu import build_task_card

    drafts = {"1.2": "let them follow the chat pick"}
    return build_task_card(_pump_rows()[1], "dv-0002", _pending_questions(), drafts)


def _menu_task_answer_prompt() -> Tuple[str, Any]:
    from ..orchestration.admin_menu import build_answer_prompt

    question = _pending_questions()["questions"][1]
    return build_answer_prompt("dv-0002", "1.2", question)


def _menu_model_choices() -> Tuple[str, Any]:
    from ..orchestration.admin_menu import build_model_choices

    return build_model_choices(
        "pump_review", "claude", "sonnet", ("sonnet", "opus", "fable")
    )


# The live shape (dv-3881 mock.models): one provider profile off the pick.
_CHAT_ROUTES = (
    "Claude · opus",
    ["scratch → OpenRouter · z-ai/glm-5.3"],
    "Claude · opus",
)


def _menu_models(
    switch: Optional[str] = None, page: int = 1
) -> Callable[[], Tuple[str, Any]]:
    def make() -> Tuple[str, Any]:
        from ...model_router.routes import load_call_routes
        from ..orchestration.admin_menu import build_models
        from ..orchestration.commands.info import ChatRoutes

        return build_models(
            Path("config/models.yaml"),
            load_call_routes(None),
            None,
            None,
            chat=ChatRoutes(*_CHAT_ROUTES),
            switch=switch,
            page=page,
        )

    return make


def _menu_calls() -> Tuple[str, Any]:
    from ...model_router.routes import load_call_routes
    from ..orchestration.admin_menu import build_calls

    return build_calls(load_call_routes(None), None)


def _menu_chat() -> Builder:
    """Модели › Чат: /model's builder through the real orchestrator."""

    async def build() -> List[Screen]:
        from ..orchestration.admin_menu import build_chat

        with _codex_state(logged_in=True):
            orch = _orchestrator()
            context = SimpleNamespace(user_data={}, bot_data={})
            start = time.perf_counter()
            text, markup = build_chat(*await orch._chat_model_screen(context, None))
            ms = (time.perf_counter() - start) * 1000
        return [Screen.from_markup("menu.chat", text, markup, "HTML", ms, MODEL_GOAL)]

    return build


def _menu_commands() -> Builder:
    """Команды: /help's renderer through the real orchestrator (dv-3881)."""

    async def build() -> List[Screen]:
        from ..orchestration.admin_menu import build_commands

        orch = _orchestrator()
        context = SimpleNamespace(user_data={}, bot_data={})
        start = time.perf_counter()
        text, markup = build_commands(orch.help_text("owner", context))
        ms = (time.perf_counter() - start) * 1000
        return [
            Screen.from_markup(
                "menu.commands",
                text,
                markup,
                "HTML",
                ms,
                "find a command that is not in the Telegram menu",
            )
        ]

    return build


def _menu_urgent_ask() -> Tuple[str, Any]:
    from ..orchestration.admin_menu import build_urgent_ask

    return build_urgent_ask("dv-0001")


def _card_screen(text: str, rows: Any) -> Tuple[str, Any]:
    """The pushed card as delivered: markdown → HTML, rows → inline keyboard."""
    from ...notifications.service import button_markup
    from ..utils.html_format import markdown_to_telegram_html

    return markdown_to_telegram_html(text), button_markup(rows)


_PUBLISH_REC = {
    "version": "1.7.0",
    "staged": "0123456789abcdef0123",
    "pending": 4,
    "entry": "- Publish card: Publish / Later buttons\n- Fixed the reminder",
}
_PUBLISH_CHECKS = ("version", "changelog", "docs", "leak", "tests", "audit", "links")


def _publish_ready() -> Tuple[str, Any]:
    from .. import publish_card

    return _card_screen(
        *publish_card.build(
            "devvy", "github.com/owner/devvy-bot", _PUBLISH_REC, _PUBLISH_CHECKS
        )
    )


def _publish_confirm() -> Tuple[str, Any]:
    from .. import publish_card

    text, _rows = publish_card.build(
        "devvy", "github.com/owner/devvy-bot", _PUBLISH_REC, _PUBLISH_CHECKS
    )
    return _card_screen(
        f"{publish_card.confirm_status('devvy')}\n\n{text}",
        publish_card.confirm_buttons("devvy"),
    )


def _restart_refused() -> Tuple[str, Any]:
    from ..orchestration.commands.system import refusal

    return refusal(
        "✘ profiles: Profile 'gymbro': extra_dirs entry is not a directory: "
        "/home/owner/gymbro-staging"
    )


def _agents_manage_off_this_run() -> Tuple[str, Any]:
    from ...profiles.registry import ProfileDefinition, ProfileRegistry
    from ..orchestration.agents_menu import build_agents_manage

    def profile(slug: str, default: bool = False) -> ProfileDefinition:
        root = Path("/home/owner/agents") / slug
        return ProfileDefinition(
            slug, slug.title(), root, root / "memory", root / "persona.md", default
        )

    file_reg = ProfileRegistry([profile("devops", True), profile("gymbro")])
    live_reg = ProfileRegistry([profile("devops", True), profile("gymbro")])
    live_reg.disable_for_run(
        "gymbro",
        "Profile 'gymbro': extra_dirs entry is not a directory: "
        "/home/owner/gymbro-staging",
    )
    return build_agents_manage(file_reg, live_reg)


def _spec(
    screen_id: str,
    goal: str,
    make: Callable[[], Tuple[str, Any]],
    known_bad: Optional[str] = None,
    parse_mode: Optional[str] = None,
) -> ScreenSpec:
    return ScreenSpec(
        screen_id, goal, _pure(screen_id, goal, make, parse_mode), known_bad
    )


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
        # Команды IS the /help text (dv-3881: one renderer), and /help names
        # every command with its line: ~60 lines, taller than one phone screen.
        # Known long until the owner picks a shorter /help (paging / folding).
        ScreenSpec(
            "menu.commands",
            "find a command that is not in the Telegram menu",
            _menu_commands(),
            known_bad="dv-3881",
        ),
        _spec(
            "menu.spend",
            "see each provider's limit, what it runs and the budget",
            _menu_spend,
        ),
        _spec(
            "menu.spend.codex_limit",
            "see which limit stops what and until when",
            _menu_spend_codex_limit,
        ),
        _spec(
            "menu.jobs_switch",
            "move all background jobs to another provider",
            _menu_jobs_switch,
        ),
        _spec(
            "menu.pump",
            "see what the Pump is doing and push one task first",
            _menu_pump,
        ),
        _spec(
            "menu.pump.frozen_quota",
            "see why the Pump is frozen and until when",
            _menu_pump_frozen_quota,
        ),
        _spec(
            "menu.task",
            "answer, push or retry one Pump task",
            _menu_task_card,
        ),
        _spec(
            "menu.task.asked",
            "read a task's open questions and answer them without the chat",
            _menu_task_asked,
        ),
        _spec(
            "menu.task.answer_prompt",
            "type or say a non-default answer to one question",
            _menu_task_answer_prompt,
        ),
        _spec(
            "menu.models",
            "see what chat, schedule and each Pump stage run on",
            _menu_models(),
        ),
        _spec(
            "menu.models.switch_on",
            "see that every stage runs on the jobs switch, not its row",
            _menu_models("openai_codex"),
        ),
        _spec(
            "menu.models.page2",
            "see and pick the models of the remaining Pump stages and jobs",
            _menu_models("openai_codex", page=2),
        ),
        _spec(
            "menu.calls",
            "see what the bot's own small calls run on",
            _menu_calls,
        ),
        ScreenSpec("menu.chat", MODEL_GOAL, _menu_chat()),
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
        _spec(
            "publish.ready",
            "publish a checked release to the public repo, or later",
            _publish_ready,
            parse_mode="HTML",
        ),
        _spec(
            "publish.confirm",
            "confirm a public release before it is pushed",
            _publish_confirm,
            parse_mode="HTML",
        ),
        ScreenSpec(
            "agents.manage.off_this_run",
            AGENTS_OFF_GOAL,
            _pure(
                "agents.manage.off_this_run",
                AGENTS_OFF_GOAL,
                _agents_manage_off_this_run,
                "HTML",
            ),
        ),
        ScreenSpec(
            "restart.refused",
            RESTART_GOAL,
            _pure("restart.refused", RESTART_GOAL, _restart_refused, "HTML"),
        ),
    )
}
