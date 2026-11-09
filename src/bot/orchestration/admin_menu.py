"""/menu — the owner's admin hub (dv-6104).

One owner command opens four buttons — Лимиты, Pump, Модели, Команды
(dv-3881: the Pump settings moved under Pump). Each opens in the same message (the callback edits it) with
« Назад back to the root. Nothing here starts an agent turn: the hub must
work when Claude's subscription is out, so every leaf is an existing
renderer or ``scripts/pump2`` run directly:

* Лимиты        — ``limits_panel`` (the /limits text, no buttons): one block
  per provider (selected chat first) with what runs on it and, on a limit,
  what waits; paid spend today / 7 days; last the /usage budget block
* Pump          — ONE ``pump2 status --json`` call (dv-f6af): "работает
  N", "Ждут тебя" as buttons that open the task's card, running and held
  tasks one line each. Freeze/Thaw (only here) go through the lim:freeze|thaw
  handler (``scripts/pump2 freeze|thaw``, dv-44b1; the head reads
  ``src/pump2/freeze.py``); Stop is the scoped /stop. The card
  (menu:task:<id>) holds the action: 🚨 urgent asks first (``pump2 urgent``:
  it freezes every board); a task with questions has "✅ всё по умолчанию" =
  ``pump2 answer <id> go``.
  dv-3881: a task with questions lists them with their defaults (``pump2
  pending --json``, the one parser); ✏ makes the owner's next text or voice
  message in that topic (10 min) a draft (``next_input``), 📨 sends the
  drafts verbatim in one ``pump2 answer``.
* Команды       — the /help text, from the same ``help_text`` (dv-3881):
  every command, unlisted ones marked ·
* Модели        — what runs on what (dv-3881): the chat pick and the
  profiles that do not follow it, the scheduled / webhook model, the jobs
  switch (top button "Фоновые задачи на: …", ``menu:jobs:*``) and the stage
  models — the ``jobs:`` rows of models.yaml (``model_router.job_rows``),
  each shown with the route that really runs (⇄ = the switch overrides it),
  same audit and ↩. "💬 Чат ▸" is /model's own screen with « Назад;
  "🔧 Внутренние вызовы ▸" lists each plain-call purpose's route, read-only.

Callback data (all owner-gated here — a button bypasses the command gate,
dv-6b6c; a guest can tap a forwarded one):

    menu:root | menu:spend | menu:pump | menu:models
    menu:commands                     → Команды: the /help text (one renderer)
    menu:models:<page>                → Модели, the stage rows of that page
    menu:limits | menu:usage          → Лимиты (buttons of older messages)
    menu:lim:freeze | menu:lim:thaw   → CallbacksMixin._handle_limits_callback
                                        (pump2 freeze|thaw --by owner)
    menu:stop                         → the scoped /stop
    menu:task:<id>                    → the task's card (state + actions)
    menu:go:<id>                      → pump2 answer <id> go
    menu:ans:<id>:<num>               → ✏ "your next message is the draft"
    menu:ansx:<id>                    → ✖ drop that wait, back to the card
    menu:send:<id>                    → 📨 pump2 answer <id> "<drafts>"
    menu:urgent:<id>                  → "freeze everything?" [Да] [Нет]
    menu:urgentgo:<id>                → pump2 urgent <board:id> --by owner
    menu:chat                         → Модели › Чат: /model's screen + « Назад
    menu:calls                        → Модели › Внутренние вызовы (read-only)
    menu:jobsw | menu:jobs:<harness|table> → the jobs switch / set it
    menu:model:<job>                  → model choices for the row's harness
    menu:mod:<job>:<model>            → set jobs.<job> (checked at tap time)
    menu:mod:undo                     → restore the last stage-model change
"""

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Collection, Dict, List, Optional, Tuple, cast

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...model_router import job_rows
from ...model_router.quota import job_route_override
from .callbacks import CallbacksMixin
from .commands.info import (
    ChatRoutes,
    InfoCommandsMixin,
    _pump_frozen,
    jobs_switch_label,
    jobs_switch_screen,
    limits_panel,
    switch_jobs,
)

logger = structlog.get_logger()

MENU_PREFIX = "menu:"
ROOT_TEXT = "🧭 Меню — без запуска модели, работает и когда лимит Claude кончился."
SETTING_EVENT = "admin_setting"
_NEXT_STAGE = "со следующего запуска стадии, без рестарта"
# Telegram caps a message at 4096 chars: every list on the Pump screen is
# capped and ends in "… ещё N".
_WAIT_BUTTONS = 12
_LIST_LINES = 8
_TITLE_CHARS = 30
_BUTTON_TITLE_CHARS = 22
_REFUSAL = "🚫 Owner only."
# dv-3881: ✏ on a question waits this long for the owner's next message.
DRAFT_WAIT_KEY = "pump_draft_wait"  # bot_data: {(chat, thread, user): record}
DRAFTS_KEY = "pump_drafts"  # user_data: {task id: {num: his words}}
DRAFT_MINUTES = 10
WHOLE_ANSWER = "all"  # the draft key when the card has no parsed questions
_QUESTION_CHARS = 150
_DEFAULT_CHARS = 60
_BLOCK_CHARS = 1500
_PENCILS_PER_ROW = 3


def _back(to: str = "root") -> List[InlineKeyboardButton]:
    return [InlineKeyboardButton("« Назад", callback_data=f"{MENU_PREFIX}{to}")]


def build_root() -> Tuple[str, InlineKeyboardMarkup]:
    return ROOT_TEXT, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Лимиты", callback_data="menu:spend"),
                InlineKeyboardButton("⚙ Pump", callback_data="menu:pump"),
            ],
            [
                InlineKeyboardButton("🧠 Модели", callback_data="menu:models"),
                InlineKeyboardButton("📋 Команды", callback_data="menu:commands"),
            ],
        ]
    )


def build_commands(help_text: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Команды (dv-3881): the /help text as is (HTML) and only « Назад."""
    return help_text, InlineKeyboardMarkup([_back()])


def build_spend(text: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Лимиты: the /limits text and only « Назад."""
    return text, InlineKeyboardMarkup([_back()])


def build_models(
    path: Path,
    routes: Any,
    error: Optional[str],
    undo: Optional[Dict[str, Any]],
    note: str = "",
    *,
    chat: Optional[ChatRoutes] = None,
    switch: Optional[str] = None,
    page: int = 1,
) -> Tuple[str, InlineKeyboardMarkup]:
    """Модели (dv-3881): what runs on what. Page 1: the jobs switch
    (``switch``: its harness, None = the table), the chat pick (Чат ▸ =
    /model's screen) with the profiles that do not follow it, the scheduled /
    webhook model, then the stage rows, ``_STAGES_PER_PAGE`` per page, each
    showing the route that really runs — ⇄ when the switch overrides the row.
    A tap edits the row (models.yaml), ↩ undoes the last change. ``routes``
    None (with ``error``) = the file does not parse: by hand."""
    lines = [note, ""] if note else []
    rows: List[List[InlineKeyboardButton]] = []
    jobs = _ordered_jobs()
    pages = max(1, -(-len(jobs) // _STAGES_PER_PAGE))
    page = min(max(page, 1), pages)
    first = (page - 1) * _STAGES_PER_PAGE
    shown = jobs[first : first + _STAGES_PER_PAGE]
    if page == 1:
        lines.append("🧠 Модели — что на чём работает")
        rows.append(
            [
                InlineKeyboardButton(
                    f"Фоновые задачи на: {jobs_switch_label(switch)} ▸",
                    callback_data="menu:jobsw",
                )
            ]
        )
        if chat is not None:
            head = f"Чат: {chat.chat}"
            lines.append(head + (" — все агенты, кроме:" if chat.others else ""))
            lines += [f"• {o}" for o in chat.others]
            lines.append(f"Расписание и вебхуки: {chat.scheduled}")
            rows.append(
                [InlineKeyboardButton(f"💬 {head} ▸", callback_data="menu:chat")]
            )
    else:
        lines.append(f"🧠 Модели — стадии {first + 1}–{first + len(shown)}")
    lines.append(f"Стадии: {path.name}, со след. запуска")
    if routes is None:
        lines.append(f"⚠ Не правлю: {error}. Правьте вручную.")
    else:
        if switch is not None:
            lines.append("⇄ = по переключателю, не по строке")
        live = replace(routes, job_switch=switch)
        for job in shown:
            model = _short_model(live.resolve_job(job).model)
            # Switched: the top button names the harness; the row says ⇄.
            label = (
                f"⇄ {job_rows.job_label(job)}: {model}"
                if switch is not None
                else f"{job_rows.job_label(job)}: "
                f"{job_rows.row_harness(routes, job)} / {model}"
            )
            rows.append(
                [InlineKeyboardButton(label, callback_data=f"menu:model:{job}")]
            )
    if undo is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    f"↩ Отменить: {job_rows.job_label(str(undo['job']))} "
                    f"{undo['new']}→{undo['old']}",
                    callback_data="menu:mod:undo",
                )
            ]
        )
    nav = []
    more = routes is not None and page < pages
    # a middle page carries both arrows: short labels, 3 buttons fit a row
    if page > 1:
        prev = f"{first - _STAGES_PER_PAGE + 1}–{first}"
        nav.append(
            InlineKeyboardButton(
                f"◂ {prev}" if more else f"◂ Стадии {prev}",
                callback_data="menu:models" if page == 2 else f"menu:models:{page - 1}",
            )
        )
    nav += _back()
    if more:
        rest = len(jobs) - first - len(shown)
        nav.append(
            InlineKeyboardButton(
                f"Ещё {rest} ▸" if page > 1 else f"Ещё {rest} стадий ▸",
                callback_data=f"menu:models:{page + 1}",
            )
        )
    if page == 1:
        rows.append(
            [InlineKeyboardButton("🔧 Внутренние вызовы ▸", callback_data="menu:calls")]
        )
    rows.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_calls(
    routes: Optional[Any], error: Optional[str]
) -> Tuple[str, InlineKeyboardMarkup]:
    """Модели › Внутренние вызовы (dv-3881): what the bot's own plain calls
    (``complete`` purposes) run on, read-only — each line is the router's
    own ``routes.resolve(purpose)``, never a copy. A purpose that follows the
    chat's backend says "как чат". ``routes`` None = the file does not parse."""
    from ...model_router.routes import FOLLOW_BACKEND, RouteRow
    from ...model_router.types import PLAIN_PURPOSES

    lines = ["🔧 Внутренние вызовы (config/models.yaml purposes, только просмотр)"]
    if routes is None:
        lines.append(f"⚠ Файл не читается: {error}.")
    else:
        for purpose in sorted(PLAIN_PURPOSES):
            row = routes.purposes.get(purpose, RouteRow())
            if (row.harness or routes.default.harness) == FOLLOW_BACKEND:
                lines.append(f"• {purpose}: как чат")
                continue
            route = routes.resolve(purpose)
            model = _short_model(route.model) if route.model else "выбирает сам"
            lines.append(f"• {purpose}: {route.harness} / {model}")
    lines.append("Правятся в файле, не из бота.")
    return "\n".join(lines), InlineKeyboardMarkup([_back("models")])


_STAGES_PER_PAGE = 5


def _ordered_jobs() -> List[str]:
    from ...model_router.types import JOB_ROUTE_NAMES, RETIRED_JOB_ROUTES

    return sorted(JOB_ROUTE_NAMES - RETIRED_JOB_ROUTES, key=_job_order)


def _models_page(job: str) -> int:
    """The Модели page a stage row is on (choices « Назад back to it)."""
    jobs = _ordered_jobs()
    return jobs.index(job) // _STAGES_PER_PAGE + 1 if job in jobs else 1


def _short_model(model: Optional[str]) -> str:
    """A job model as the row shows it: Codex's ``openai-codex/`` prefix off
    (the harness already says Codex)."""
    return str(model).removeprefix("openai-codex/") if model else "—"


def build_chat(
    text: str, markup: InlineKeyboardMarkup
) -> Tuple[str, InlineKeyboardMarkup]:
    """Модели › Чат: /model's own screen (``_chat_model_screen``) plus
    « Назад to Модели — never a second picker."""
    return text, InlineKeyboardMarkup([*markup.inline_keyboard, _back("models")])


def build_model_choices(
    job: str,
    harness: str,
    current: Optional[str],
    choices: Tuple[str, ...],
    switch: Optional[str] = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    """The models a stage may run on, for the row's harness. ``switch`` on:
    the row is edited but applies only once the switch is back to the table."""
    rows: List[List[InlineKeyboardButton]] = []
    for model in choices:
        data = f"menu:mod:{job}:{model}"
        if len(data.encode("utf-8")) > 64:  # Telegram's callback_data cap
            continue
        mark = "✓ " if model == current else ""
        rows.append([InlineKeyboardButton(f"{mark}{model}", callback_data=data)])
    page = _models_page(job)
    rows.append(_back("models" if page == 1 else f"models:{page}"))
    text = (
        f"🧠 {job_rows.job_label(job)} (jobs.{job}): {harness} / {current or '—'}\n"
        f"Выберите модель для {harness} — {_NEXT_STAGE}."
    )
    if switch is not None:
        text += (
            f"\n⇄ Сейчас все стадии на {jobs_switch_label(switch)}: эта строка "
            f"заработает, когда фоновые задачи вернутся «{jobs_switch_label(None)}»."
        )
    return text, InlineKeyboardMarkup(rows)


def _job_order(job: str) -> Tuple[int, str]:
    order = list(job_rows.JOB_LABELS)
    return (order.index(job) if job in order else len(order), job)


def _short(title: Any, limit: int = _TITLE_CHARS) -> str:
    text = str(title or "").strip("\"' ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Quota providers by their record names (dv-47f9) in the owner's words.
_PROVIDER_WORDS = {"anthropic": "Claude", "openai": "Codex", "openrouter": "OpenRouter"}
_HELD_LIMIT_RE = re.compile(r"(?P<provider>[\w-]+) usage limit until (?P<until>.+)")


def _frozen_by(frozen: Dict[str, Any]) -> str:
    """Who froze Pump: the owner, or the limit that did and until when
    (only the Claude limit freezes it; a Codex one holds its stages)."""
    by = str(frozen.get("by") or "?")
    if by != "quota":
        return by
    who = _PROVIDER_WORDS.get(str(frozen.get("provider") or "anthropic"), "Claude")
    until = _until_hhmm(frozen.get("until"))
    return f"лимит {who}" + (f" до {until}" if until else "")


def _until_hhmm(value: Any) -> str:
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    at = at.astimezone(UTC)
    same_day = at.date() == datetime.now(UTC).date()
    return at.strftime("%H:%M" if same_day else "%d.%m %H:%M")


def _blocker(why: str) -> str:
    """The script's English hold reason in the owner's words."""
    if why.startswith("held: "):
        m = _HELD_LIMIT_RE.fullmatch(why[len("held: ") :])
        if m:
            who = _PROVIDER_WORDS.get(m["provider"], m["provider"])
            return f"держит: лимит {who} до {m['until']}"
    for en, ru in (("waits ", "ждёт "), ("starts ", "старт "), ("held: ", "держит: ")):
        if why.startswith(en):
            return ru + why[len(en) :]
    if why.startswith("answer "):
        return "ответь на " + why[len("answer ") :]
    if why == "failed":
        return "упала"
    if why.startswith("blocked: "):
        return "заблокирована: " + why[len("blocked: ") :]
    m = re.match(r"cap (\d+)/(\d+) before (.+)", why)
    if m:
        return f"лимит запусков {m.group(1)}/{m.group(2)} перед {m.group(3)}"
    return why


def _is_waiting_for_owner(t: Dict[str, Any]) -> bool:
    return t.get("group") == "needs"


def _more(rows: List[Any], limit: int) -> str:
    return f"… ещё {len(rows) - limit}" if len(rows) > limit else ""


def build_pump(
    tasks: List[Dict[str, Any]],
    frozen: Optional[Dict[str, Any]],
) -> Tuple[str, InlineKeyboardMarkup]:
    """The Pump screen, from one ``status --json`` list: running N, the tasks
    waiting for the owner as buttons (→ the task's card), running and held
    tasks one line each, Freeze or Thaw (only here), Stop, « Назад."""
    by_group: Dict[str, List[Dict[str, Any]]] = {}
    for t in tasks:
        by_group.setdefault(str(t.get("group") or ""), []).append(t)
    running = by_group.get("running", [])
    waiting_you = [t for t in tasks if _is_waiting_for_owner(t)]
    held = by_group.get("waiting", []) + by_group.get("stopped", [])
    queued = by_group.get("queued", [])

    if frozen is None:
        head = f"⚙ Pump · работает {len(running)}"
    else:
        # A limit's freeze names the limit and its reset; the head stays one
        # phone line, so "работает" goes (the bare count still reads).
        by, busy = _frozen_by(frozen), str(len(running))
        head = (
            f"❄ Pump заморожен: {by} · {busy}"
            if frozen.get("by") == "quota"
            else f"❄ Pump заморожен ({by}) · работает {busy}"
        )
    lines = [head]
    rows: List[List[InlineKeyboardButton]] = []
    if waiting_you:
        lines += ["", f"Ждут тебя ({len(waiting_you)}):"]
        for t in waiting_you[:_WAIT_BUTTONS]:
            tid = str(t["id"])
            icon = str(t.get("icon") or "") or "❓"
            n = _open_count(t)
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{icon} {tid} {_short(t.get('title'), _BUTTON_TITLE_CHARS)}"
                        + (f" · {n}" if n else ""),
                        callback_data=f"{MENU_PREFIX}task:{tid}",
                    )
                ]
            )
        if len(waiting_you) > _WAIT_BUTTONS:
            lines.append(_more(waiting_you, _WAIT_BUTTONS))
    if running:
        lines += ["", "Работают:"]
        lines += [
            f"▶ {t['id']} · {t.get('why') or ''} — {_short(t.get('title'))}"
            for t in running[:_LIST_LINES]
        ]
        lines += [m for m in (_more(running, _LIST_LINES),) if m]
    if held:
        lines += ["", f"Держатся ({len(held)}):"]
        lines += [
            f"{t.get('icon') or '⏸'} {t['id']} · {_blocker(str(t.get('why') or ''))}"
            f" — {_short(t.get('title'), 24)}"
            for t in held[:_LIST_LINES]
        ]
        lines += [m for m in (_more(held, _LIST_LINES),) if m]
    if queued:
        first = ", ".join(str(t["id"]) for t in queued[:3])
        lines += [
            "",
            f"В очереди ({len(queued)}): {first}" + (" …" if len(queued) > 3 else ""),
        ]
    if not (running or waiting_you or held or queued):
        lines += ["", "Ничего не ждёт и не работает."]

    rows.append(
        [
            (
                InlineKeyboardButton(
                    "▶ Разморозить Pump", callback_data="menu:lim:thaw"
                )
                if frozen is not None
                else InlineKeyboardButton(
                    "⏸ Заморозить Pump", callback_data="menu:lim:freeze"
                )
            )
        ]
    )
    rows.append([InlineKeyboardButton("🛑 Стоп этого чата", callback_data="menu:stop")])
    rows.append(_back())
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _open_count(t: Dict[str, Any]) -> int:
    """Open questions of an asked row, from its "answer 1, 2" why (the
    status row carries pump_open only there)."""
    why = str(t.get("why") or "")
    if t.get("pump") != "asked" or not why.startswith("answer "):
        return 0
    nums = why[len("answer ") :]
    return 0 if nums == "questions" else len([n for n in nums.split(",") if n.strip()])


def compose_answer(questions: List[Dict[str, Any]], drafts: Dict[str, str]) -> str:
    """The one ``pump2 answer`` text: "<num> <his words>; …; остальное по
    умолчанию". His words go in verbatim; only drafts of this card's
    questions count (a stale one from an earlier round does not)."""
    if WHOLE_ANSWER in drafts:
        return drafts[WHOLE_ANSWER]
    nums = [str(q.get("num")) for q in questions]
    parts = [f"{n} {drafts[n]}" for n in nums if n in drafts]
    if any(n not in drafts for n in nums):
        parts.append("остальное по умолчанию")
    return "; ".join(parts)


def _card_drafts(
    questions: List[Dict[str, Any]], drafts: Optional[Dict[str, str]]
) -> Dict[str, str]:
    nums = {str(q.get("num")) for q in questions} | {WHOLE_ANSWER}
    return {k: v for k, v in (drafts or {}).items() if k in nums and v}


def _question_lines(
    pending: Dict[str, Any], drafts: Dict[str, str]
) -> Tuple[List[str], List[List[InlineKeyboardButton]]]:
    """The asked card's body: each question with its default (✏ per open
    one, its draft under it), or the raw question block when pump2 could
    not parse one (one ✏ for a whole answer)."""
    tid = str(pending.get("id"))
    questions = [q for q in pending.get("questions") or [] if isinstance(q, dict)]
    lines: List[str] = []
    pencils: List[InlineKeyboardButton] = []
    open_qs = [q for q in questions if not q.get("decided")]
    if questions:
        lines.append(f"Ждёт ответа · открытых вопросов: {len(open_qs)}")
        for q in questions:
            num = str(q.get("num"))
            text = _short(q.get("text"), _QUESTION_CHARS)
            default = _short(q.get("default") or "—", _DEFAULT_CHARS)
            if q.get("decided"):
                lines.append(f"{num} {text} — решено: {default}")
                continue
            lines.append(f"{num} {text} — по умолч.: {default}")
            if num in drafts:
                lines.append(f"   ✏ «{_short(drafts[num], _QUESTION_CHARS)}»")
            mark = " ✓" if num in drafts else ""
            pencils.append(
                InlineKeyboardButton(
                    f"✏ {num}{mark}", callback_data=f"{MENU_PREFIX}ans:{tid}:{num}"
                )
            )
    else:
        block = str(pending.get("block") or "").strip()
        lines.append("Ждёт ответа:")
        if block:
            lines.append(
                block[:_BLOCK_CHARS] + ("…" if len(block) > _BLOCK_CHARS else "")
            )
        if WHOLE_ANSWER in drafts:
            lines.append(f"   ✏ «{_short(drafts[WHOLE_ANSWER], _QUESTION_CHARS)}»")
        pencils.append(
            InlineKeyboardButton(
                "✏ Ответить" + (" ✓" if WHOLE_ANSWER in drafts else ""),
                callback_data=f"{MENU_PREFIX}ans:{tid}:{WHOLE_ANSWER}",
            )
        )
    if drafts:
        lines.append(f"Уйдёт: «{_short(compose_answer(questions, drafts), 300)}»")
    rows = [
        pencils[i : i + _PENCILS_PER_ROW]
        for i in range(0, len(pencils), _PENCILS_PER_ROW)
    ]
    if drafts:
        rows.append(
            [
                InlineKeyboardButton(
                    "📨 Отправить ответ", callback_data=f"{MENU_PREFIX}send:{tid}"
                )
            ]
        )
    return lines, rows


def build_task_card(
    task: Optional[Dict[str, Any]],
    task_id: str,
    pending: Optional[Dict[str, Any]] = None,
    drafts: Optional[Dict[str, str]] = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    """One task: its state, and what can be done with it. 🚨 unless done or
    blocked; "всё по умолчанию" on a task with questions (``pump2 answer <id>
    go``).

    dv-3881: ``pending`` (its ``pending --json`` row) lists an asked task's
    questions with ✏ per question and 📨 once ``drafts`` hold his words."""
    back = [_back("pump")]
    if task is None:
        return (
            f"{task_id}: не нахожу в Pump (уже ушла из конвейера?).",
            InlineKeyboardMarkup(back),
        )
    lines = [f"{task_id} — {_short(task.get('title'), 80)}"]
    state = str(task.get("why") or task.get("pump") or "")
    pump = task.get("pump")
    rows: List[List[InlineKeyboardButton]] = []
    if pump == "asked" and pending is not None:
        more, buttons = _question_lines(
            pending,
            _card_drafts(list(pending.get("questions") or []), drafts),
        )
        lines += more
        rows += buttons
    else:
        lines.append(f"Состояние: {_blocker(state)}{task.get('act') or ''}")
    if pump == "asked":
        if pending is None:
            lines.append(
                f"Ответь сообщением: «{task_id}: 1 default, 2 нет» — или одной кнопкой:"
            )
        rows.append(
            [
                InlineKeyboardButton(
                    "✅ Всё по умолчанию", callback_data=f"{MENU_PREFIX}go:{task_id}"
                )
            ]
        )
    # 🚨 only where the pump would run the task: urgent refuses a stopped one.
    if pump not in ("done", "blocked", "stopped"):
        rows.append(
            [
                InlineKeyboardButton(
                    "🚨 Срочно", callback_data=f"{MENU_PREFIX}urgent:{task_id}"
                )
            ]
        )
    rows.append(_back("pump"))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_answer_prompt(
    task_id: str, num: str, question: Optional[Dict[str, Any]]
) -> Tuple[str, InlineKeyboardMarkup]:
    """✏ on a question: his next text or voice message here is the draft."""
    lines = [f"✏ {task_id} · {num if num != WHOLE_ANSWER else 'ответ'}"]
    if question:
        lines.append(_short(question.get("text"), _QUESTION_CHARS))
        if question.get("default"):
            lines.append(f"по умолч.: {_short(question['default'], _DEFAULT_CHARS)}")
    lines += [
        "",
        f"Ответь текстом или голосом в этой теме ({DRAFT_MINUTES} мин) —",
        "ответ встанет в черновик, отправишь кнопкой 📨.",
    ]
    return "\n".join(lines), InlineKeyboardMarkup(
        [[InlineKeyboardButton("✖ Отмена", callback_data=f"menu:ansx:{task_id}")]]
    )


def build_urgent_ask(task_id: str) -> Tuple[str, InlineKeyboardMarkup]:
    """🚨 freezes every board — one more tap before it runs."""
    return (
        f"🚨 {task_id}: заморозить весь Pump и запустить только эту задачу "
        "(мимо капов и зависимостей)?",
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Да, срочно", callback_data=f"menu:urgentgo:{task_id}"
                    ),
                    InlineKeyboardButton("✖ Нет", callback_data="menu:pump"),
                ]
            ]
        ),
    )


def _thread_of(message: Any) -> Optional[int]:
    """The topic a message is in, by the routing rule (routing.py)."""
    if message is None or not getattr(message, "is_topic_message", False):
        return None
    thread = getattr(message, "message_thread_id", None)
    return thread if isinstance(thread, int) else None


def _draft_key(chat_id: Any, message: Any, user_id: Any) -> Tuple[Any, ...]:
    """A ✏ wait belongs to one chat, one topic, one user: in the forum
    supergroup every agent topic shares the chat id (dv-3881 finding 3)."""
    return (chat_id, _thread_of(message), user_id)


def _drafts(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, str]]:
    drafts: Dict[str, Dict[str, str]] = context.user_data.setdefault(  # type: ignore[union-attr]
        DRAFTS_KEY, {}
    )
    return drafts


def _waits(context: ContextTypes.DEFAULT_TYPE) -> Dict[Tuple[Any, ...], Dict[str, Any]]:
    waits: Dict[Tuple[Any, ...], Dict[str, Any]] = context.bot_data.setdefault(
        DRAFT_WAIT_KEY, {}
    )
    return waits


def _expired(record: Dict[str, Any]) -> bool:
    at = record.get("expires_at")
    if isinstance(at, datetime):
        return at <= datetime.now(UTC)
    if isinstance(at, (int, float)):
        return at <= datetime.now(UTC).timestamp()
    return True


def _json_list(out: str) -> Optional[List[Any]]:
    """A ``--json`` list: the whole output (indented JSON), else its last
    line (a note printed before it); None when neither parses."""
    lines = out.strip().splitlines()
    for chunk in (out, lines[-1] if lines else "[]"):
        try:
            rows = json.loads(chunk)
        except ValueError:
            continue
        return rows if isinstance(rows, list) else []
    return None


async def pump_write(*argv: str) -> "Tuple[int, str, str]":
    """One Pump-screen write on a card (argv[1]): (rc, output, script).
    Pump v2 takes ``answer`` (his words after ``--``) and ``urgent``
    (dv-44b1); any other write is not in pump2 v1 (dv-d474)."""
    from ...claude.agent_tools import PUMP2_NOT_V1, pump_card_route, run_repo_script

    verb = argv[0] if argv else ""
    _, cid = pump_card_route(argv[1]) if len(argv) > 1 else ("pump2", "")
    if verb == "urgent":
        rc, out = await run_repo_script("pump2", "urgent", cid, *argv[2:])
        return rc, out, "pump2"
    if verb == "answer":
        rc, out = await run_repo_script("pump2", "answer", cid, "--", *argv[2:])
        return rc, out, "pump2"
    return 2, f"{verb} is {PUMP2_NOT_V1}: {cid} runs in Pump v2", "pump2"


async def _pending_row(task_id: str) -> Optional[Dict[str, Any]]:
    """The task's ``pump2 pending --json`` row; None if absent/failed."""
    from ...claude.agent_tools import pump_card_route, run_repo_script

    _, cid = pump_card_route(task_id)
    rc, out = await run_repo_script("pump2", "pending", "--json")
    if rc != 0:
        return None
    rows = _json_list(out)
    if rows is None:
        return None
    for row in rows:
        if isinstance(row, dict) and str(row.get("id")) == cid:
            return row
    return None


def _pending_task(pending: Dict[str, Any]) -> Dict[str, Any]:
    """The card's status fields from a pending row (no status call)."""
    return {
        "id": pending.get("id"),
        "title": pending.get("title"),
        "board": pending.get("board"),
        "pump": pending.get("pump"),
        "group": "needs",
    }


async def take_pump_draft(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> bool:
    """``next_input``'s Pump consumer: after ✏, the owner's next text or
    voice transcript in the same chat + topic, within DRAFT_MINUTES, is his
    draft for that question, verbatim — no agent turn — and the card comes
    back with 📨. Another topic, an expired or no wait: not consumed."""
    waits = context.bot_data.get(DRAFT_WAIT_KEY)
    if not waits:
        return False
    message: Any = update.message
    user = update.effective_user
    if message is None or user is None:
        return False
    key = _draft_key(message.chat.id, message, user.id)
    record = waits.get(key)
    if record is None:
        return False
    waits.pop(key, None)
    if _expired(record):
        return False
    task_id, num = str(record["task"]), str(record["num"])
    _drafts(context).setdefault(task_id, {})[num] = text
    logger.info("menu: pump draft taken", task=task_id, num=num, chars=len(text))
    pending = await _pending_row(task_id)
    if pending is None:
        await message.reply_text(
            f"✏ {task_id} · {num}: черновик сохранён, но вопросы задачи сейчас не "
            "читаются — открой её в /menu → Pump."
        )
        return True
    card, markup = build_task_card(
        _pending_task(pending), task_id, pending, _drafts(context).get(task_id)
    )
    await message.reply_text(card, reply_markup=markup)
    return True


def _status_rows(out: str) -> List[Dict[str, Any]]:
    try:
        rows = json.loads(out)
    except ValueError:
        return []
    return (
        [r for r in rows if isinstance(r, dict) and r.get("id")]
        if isinstance(rows, list)
        else []
    )


class AdminMenuMixin(InfoCommandsMixin, CallbacksMixin):
    """/menu and its ``menu:*`` buttons."""

    async def agentic_menu(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """/menu (owner-only via OWNER_ONLY_COMMANDS): the root screen."""
        text, markup = build_root()
        message: Any = update.message
        await message.reply_text(text, reply_markup=markup)

    async def _menu_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query: Any = update.callback_query
        data = query.data or ""
        await query.answer()
        if self._resolve_role(query.from_user.id, context) != "owner":
            logger.warning("menu: non-owner tap refused", data=data)
            await query.edit_message_text(_REFUSAL)
            return
        action, _, arg = data[len(MENU_PREFIX) :].partition(":")

        if action == "root":
            await self._menu_edit(query, *build_root())
        elif action in ("spend", "limits", "usage"):
            # limits / usage: buttons of messages sent before v2.
            text, _ = await limits_panel(context, self, query.from_user.id)
            await self._menu_edit(query, *build_spend(text))
        elif action == "pump":
            await self._menu_pump(query)
        elif action == "models":
            page = int(arg) if arg.isdigit() else 1
            await self._menu_models(query, context, page=page)
        elif action == "calls":
            await self._menu_calls(query, context)
        elif action == "commands":
            # The same renderer as /help; the tapper is the owner (checked above).
            help_text = cast(Any, self).help_text("owner", context)
            await self._menu_edit(query, *build_commands(help_text), parse_mode="HTML")
        elif action == "chat":
            text, markup = await self._chat_model_screen(context, query.from_user.id)
            await self._menu_edit(query, *build_chat(text, markup), parse_mode="HTML")
        elif action == "jobsw":
            await self._menu_edit(query, *self._jobs_screen(query))
        elif action == "jobs" and arg:
            note = switch_jobs(arg, str(query.from_user.id))
            await self._menu_edit(query, *self._jobs_screen(query, note))
        elif action == "model" and arg:
            await self._menu_model_choices(query, context, arg)
        elif action == "mod" and arg:
            await self._menu_mod(query, context, arg)
        elif action == "lim" and arg in ("freeze", "thaw"):
            await self._handle_limits_callback(
                query, context, f"lim:{arg}", InlineKeyboardMarkup([_back("pump")])
            )
        elif action == "stop":
            text = await self._stop_this_chat(update, context)
            await self._menu_edit(
                query, text, InlineKeyboardMarkup([_back("pump")]), parse_mode="HTML"
            )
        elif action == "task" and arg:
            await self._menu_task(query, context, arg)
        elif action == "go" and arg:
            _drafts(context).pop(arg, None)
            await self._menu_pump_write(query, "answer", arg, "go")
        elif action == "ans" and arg:
            await self._menu_answer_prompt(query, context, arg)
        elif action == "ansx" and arg:
            _waits(context).pop(
                _draft_key(query.message.chat.id, query.message, query.from_user.id),
                None,
            )
            await self._menu_task(query, context, arg)
        elif action == "send" and arg:
            await self._menu_send(query, context, arg)
        elif action == "urgent" and arg:
            await self._menu_edit(query, *build_urgent_ask(arg))
        elif action == "urgentgo" and arg:
            await self._menu_pump_write(query, "urgent", arg, "--by", "owner")
        else:
            await self._menu_edit(
                query, "Unknown menu action.", InlineKeyboardMarkup([_back()])
            )

    @staticmethod
    async def _menu_edit(
        query: Any,
        text: str,
        markup: InlineKeyboardMarkup,
        parse_mode: Optional[str] = None,
    ) -> None:
        """One screen = one edit of the menu message (no new messages)."""
        try:
            await query.edit_message_text(
                text, reply_markup=markup, parse_mode=parse_mode
            )
        except Exception as exc:
            if "not modified" in str(exc).lower():  # a double tap
                return
            # The menu message may be gone; the tap must still show (dv-b399).
            logger.warning("menu: edit failed", error=str(exc))
            try:
                await query.message.reply_text(text, reply_markup=markup)
            except Exception:
                pass

    @staticmethod
    def _jobs_screen(query: Any, note: str = "") -> Tuple[str, InlineKeyboardMarkup]:
        """The jobs switch with « Назад to Модели."""
        text, markup = jobs_switch_screen(note)
        return text, InlineKeyboardMarkup([*markup.inline_keyboard, _back("models")])

    @staticmethod
    async def _status_rows() -> Optional[List[Dict[str, Any]]]:
        """``pump2 status --json``; None if it failed."""
        from ...claude.agent_tools import run_repo_script

        rc, out = await run_repo_script("pump2", "status", "--json")
        return _status_rows(out) if rc == 0 else None

    async def _menu_pump(self, query: Any) -> None:
        rows = await self._status_rows()
        if rows is None:
            await self._menu_edit(
                query,
                "⚙ Pump\n\npump2 status не ответил — попробуй ещё раз.",
                InlineKeyboardMarkup([_back()]),
            )
            return
        await self._menu_edit(query, *build_pump(rows, _pump_frozen()))

    async def _menu_task(
        self, query: Any, context: ContextTypes.DEFAULT_TYPE, task_id: str
    ) -> None:
        rows = await self._status_rows() or []
        task = next((t for t in rows if str(t.get("id")) == task_id), None)
        pending = None
        if task is not None and task.get("pump") == "asked":
            pending = await _pending_row(task_id)
        await self._menu_edit(
            query,
            *build_task_card(task, task_id, pending, _drafts(context).get(task_id)),
        )

    async def _menu_answer_prompt(
        self, query: Any, context: ContextTypes.DEFAULT_TYPE, arg: str
    ) -> None:
        """✏: register the wait for this chat + topic + user (a new ✏
        replaces the old one) and show the question with ✖."""
        task_id, _, num = arg.rpartition(":")
        if not task_id or not num:
            await self._menu_edit(
                query, "Неизвестная кнопка.", InlineKeyboardMarkup([_back("pump")])
            )
            return
        pending = await _pending_row(task_id)
        question = next(
            (
                q
                for q in (pending or {}).get("questions") or []
                if isinstance(q, dict) and str(q.get("num")) == num
            ),
            None,
        )
        _waits(context)[
            _draft_key(query.message.chat.id, query.message, query.from_user.id)
        ] = {
            "task": task_id,
            "num": num,
            "expires_at": datetime.now(UTC) + timedelta(minutes=DRAFT_MINUTES),
        }
        await self._menu_edit(query, *build_answer_prompt(task_id, num, question))

    async def _menu_send(
        self, query: Any, context: ContextTypes.DEFAULT_TYPE, task_id: str
    ) -> None:
        """📨: one ``answer`` (``pump_write``) with the drafts, his words
        verbatim. A failure keeps the drafts (re-send after fixing)."""
        pending = await _pending_row(task_id)
        questions = list((pending or {}).get("questions") or [])
        drafts = _card_drafts(questions, _drafts(context).get(task_id))
        if not drafts:
            await self._menu_task(query, context, task_id)
            return
        rc, out, script = await pump_write(
            "answer", task_id, compose_answer(questions, drafts)
        )
        logger.info("menu: pump answer (drafts)", task=task_id, rc=rc, script=script)
        if rc == 0:
            _drafts(context).pop(task_id, None)
            head = "✅"
        else:
            head = f"❌ {script} answer failed (exit {rc}); черновик сохранён."
        await self._menu_edit(
            query,
            f"{head}\n{out[-800:]}".strip(),
            InlineKeyboardMarkup([_back("pump")]),
        )

    async def _menu_pump_write(self, query: Any, *argv: str) -> None:
        """Run one pump write (``pump_write``) and show its output, « Назад."""
        rc, out, script = await pump_write(*argv)
        logger.info("menu: pump write", argv=list(argv), rc=rc, script=script)
        head = "✅" if rc == 0 else f"❌ {script} {argv[0]} failed (exit {rc})."
        await self._menu_edit(
            query,
            f"{head}\n{out[-800:]}".strip(),
            InlineKeyboardMarkup([_back("pump")]),
        )

    # --- Настройки → Модели: the jobs: rows of models.yaml ---------------------

    @staticmethod
    def _routes_path(context: ContextTypes.DEFAULT_TYPE) -> Path:
        settings = context.bot_data.get("settings")
        return job_rows.routes_path(getattr(settings, "model_routes_path", None))

    async def _menu_models(
        self,
        query: Any,
        context: ContextTypes.DEFAULT_TYPE,
        note: str = "",
        page: int = 1,
    ) -> None:
        path = self._routes_path(context)
        routes = None
        error: Optional[str] = None
        undo = None
        try:
            routes = job_rows.load(path)
        except job_rows.RowError as exc:
            error = str(exc)
        if routes is not None:
            event = await _last_change(
                context, query.from_user.id, str(path), _model_names()
            )
            undo = event.event_data if event is not None else None
        await self._menu_edit(
            query,
            *build_models(
                path,
                routes,
                error,
                undo,
                note,
                chat=self.chat_routes(context, query.from_user.id),
                switch=job_route_override(),
                page=page,
            ),
        )

    async def _menu_calls(self, query: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
        routes = None
        error: Optional[str] = None
        try:
            routes = job_rows.load(self._routes_path(context))
        except job_rows.RowError as exc:
            error = str(exc)
        await self._menu_edit(query, *build_calls(routes, error))

    async def _menu_model_choices(
        self, query: Any, context: ContextTypes.DEFAULT_TYPE, job: str
    ) -> None:
        from ...model_router.types import JOB_ROUTE_NAMES

        if job not in JOB_ROUTE_NAMES:
            await self._menu_models(query, context, "Неизвестная кнопка.")
            return
        try:
            routes = job_rows.load(self._routes_path(context))
        except job_rows.RowError:
            await self._menu_models(query, context)
            return
        harness = job_rows.row_harness(routes, job)
        await self._menu_edit(
            query,
            *build_model_choices(
                job,
                harness,
                job_rows.row_model(routes, job),
                job_rows.model_choices(harness, routes),
                job_route_override(),
            ),
        )

    async def _menu_mod(
        self, query: Any, context: ContextTypes.DEFAULT_TYPE, arg: str
    ) -> None:
        """Set one stage's model, or ↩ the last change: re-read the file,
        check against the row's harness now, write, log old→new. A refusal
        writes nothing."""
        path = self._routes_path(context)
        user_id = query.from_user.id
        undo_of = None
        try:
            if arg == "undo":
                event = await _last_change(context, user_id, str(path), _model_names())
                if event is None:
                    await self._menu_models(query, context, "Нечего отменять.")
                    return
                data = event.event_data
                change = job_rows.undo_job_model(
                    path,
                    job_rows.Change(
                        job=str(data["job"]),
                        harness=str(data["harness"]),
                        old=data.get("old"),
                        new=data.get("new"),
                        unset=bool(data.get("unset")),
                        created=bool(data.get("created")),
                    ),
                )
                undo_of = event.id
            else:
                job, _, model = arg.partition(":")
                done = job_rows.set_job_model(path, job, model)
                if done is None:
                    await self._menu_models(
                        query,
                        context,
                        f"{job_rows.job_label(job)}: уже {model}.",
                        page=_models_page(job),
                    )
                    return
                change = done
        except job_rows.RowError as exc:
            logger.warning("menu: model row refused", path=str(path), error=str(exc))
            await self._menu_models(query, context, f"⛔ {exc}")
            return

        record: Dict[str, Any] = {
            "name": f"jobs.{change.job}",
            "job": change.job,
            "harness": change.harness,
            "old": change.old,
            "new": change.new,
            "source": str(path),
            "unset": change.unset,
            "created": change.created,
        }
        if undo_of is not None:
            record["undo"] = undo_of
        logger.info("menu: admin setting", user_id=user_id, **record)
        await _audit(context, user_id, record)
        await self._menu_models(
            query,
            context,
            f"✅ {job_rows.job_label(change.job)} ({change.harness}): "
            f"{change.old} → {change.new}, {_NEXT_STAGE}.",
            page=_models_page(change.job),
        )


def _model_names() -> List[str]:
    from ...model_router.types import JOB_ROUTE_NAMES

    return [f"jobs.{job}" for job in JOB_ROUTE_NAMES]


async def _audit(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, record: Dict[str, Any]
) -> None:
    """One ``admin_setting`` bot event (who/when/old→new); a failed write
    only warns — the change is already made."""
    storage = context.bot_data.get("storage")
    if storage is None:
        return
    try:
        await storage.log_bot_event(user_id, SETTING_EVENT, record)
    except Exception as exc:
        logger.warning("menu: audit write failed", error=str(exc))


async def _last_change(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    source: str,
    names: Collection[str],
) -> Any:
    """The newest ``admin_setting`` change of ``names`` in ``source`` not yet
    undone (undo is a stack: each undo row cancels the newest change before
    it)."""
    storage = context.bot_data.get("storage")
    if storage is None:
        return None
    try:
        rows = await storage.audit.get_user_audit_log(user_id, limit=200)
    except Exception as exc:
        logger.warning("menu: audit read failed", error=str(exc))
        return None
    events = sorted(
        (
            r
            for r in rows
            if r.event_type == SETTING_EVENT
            and isinstance(r.event_data, dict)
            and r.event_data.get("source") == source
            and r.event_data.get("name") in names
        ),
        key=lambda r: r.id or 0,
        reverse=True,
    )
    pending = 0
    for event in events:
        if event.event_data.get("undo"):
            pending += 1
        elif pending:
            pending -= 1
        else:
            return event
    return None
