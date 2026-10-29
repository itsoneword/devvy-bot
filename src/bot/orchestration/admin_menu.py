"""/menu — the owner's admin hub (dv-6104).

One owner command opens four buttons — Расход, Pump, Модели, Настройки. Each opens in the same message (the callback edits it) with
« Назад back to the root. Nothing here starts an agent turn: the hub must
work when Claude's subscription is out, so every leaf is an existing
renderer or ``scripts/taskpump`` run directly:

* Расход        — ``limits_panel`` (the /limits text, no buttons): Claude,
  paid spend today / 7 days, Codex only if used this week (dv-f6af)
* Pump          — ONE ``taskpump status --json`` call (dv-f6af): "работает
  N/max", "Ждут тебя" as buttons that open the task's card, running and held
  tasks one line each. Freeze/Thaw (only here) go through the lim:freeze|thaw
  handler; Stop is the scoped /stop. The card (menu:task:<id>) holds the
  actions: ⚡ first runs at once, 🚨 urgent asks first (it freezes every
  board), "✅ всё по умолчанию" / "▶ Ещё 5" = ``taskpump answer <id> go``.
* Настройки     — the Pump caps in the taskpump user unit (``pump_unit``):
  −/+ per value, validated, logged as ``admin_setting``, ↩ undoes the last
  change.
* Модели        — the stage models: the ``jobs:`` rows of models.yaml
  (``model_router.job_rows``), same audit and ↩, under a top button
  "Фоновые задачи на: …" that opens the jobs switch (``menu:jobs:*``).

Callback data (all owner-gated here — a button bypasses the command gate,
dv-6b6c; a guest can tap a forwarded one):

    menu:root | menu:spend | menu:pump | menu:models | menu:settings
    menu:limits | menu:usage          → Расход (buttons of older messages)
    menu:lim:freeze | menu:lim:thaw   → CallbacksMixin._handle_limits_callback
    menu:stop                         → the scoped /stop
    menu:task:<id>                    → the task's card (state + actions)
    menu:go:<id>                      → taskpump answer <id> go
    menu:retry:<id>                   → taskpump retry <id>
    menu:first:<id>                   → taskpump first <id> --why … --by owner
    menu:urgent:<id>                  → "freeze everything?" [Да] [Нет]
    menu:urgentgo:<id>                → taskpump urgent <id> --by owner
    menu:set:<NAME>:+|-               → one step of a Pump cap in the unit
    menu:set:undo                     → restore the last admin_setting change
    menu:jobsw | menu:jobs:<harness|table> → the jobs switch / set it
    menu:model:<job>                  → model choices for the row's harness
    menu:mod:<job>:<model>            → set jobs.<job> (checked at tap time)
    menu:mod:undo                     → restore the last stage-model change
"""

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Collection, Dict, List, Optional, Tuple

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...config import pump_unit
from ...model_router import job_rows
from .callbacks import CallbacksMixin
from .commands.info import (
    InfoCommandsMixin,
    _pump_frozen,
    jobs_switch_name,
    jobs_switch_screen,
    limits_panel,
    switch_jobs,
)

logger = structlog.get_logger()

MENU_PREFIX = "menu:"
ROOT_TEXT = "🧭 Меню — без запуска модели, работает и когда лимит Claude кончился."
SETTING_EVENT = "admin_setting"
_NEXT_TICK = "со следующего тика Pump"
_NEXT_STAGE = "со следующего запуска стадии, без рестарта"
_MEMORY_NOTE = (
    "Каждый параллельный запуск — отдельный процесс claude: " "поднимайте по одному."
)
# Telegram caps a message at 4096 chars: every list on the Pump screen is
# capped and ends in "… ещё N".
_WAIT_BUTTONS = 12
_LIST_LINES = 8
_REVIEW_WAIT_HOURS = 6  # a finished card older than this waits for the owner
_TITLE_CHARS = 30
_BUTTON_TITLE_CHARS = 22
_WHY_FIRST = "from /menu"
_REFUSAL = "🚫 Owner only."


def _back(to: str = "root") -> List[InlineKeyboardButton]:
    return [InlineKeyboardButton("« Назад", callback_data=f"{MENU_PREFIX}{to}")]


def build_root() -> Tuple[str, InlineKeyboardMarkup]:
    return ROOT_TEXT, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Расход", callback_data="menu:spend"),
                InlineKeyboardButton("⚙ Pump", callback_data="menu:pump"),
            ],
            [
                InlineKeyboardButton("🧠 Модели", callback_data="menu:models"),
                InlineKeyboardButton("🛠 Настройки", callback_data="menu:settings"),
            ],
        ]
    )


def build_spend(text: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Расход: the /limits text and only « Назад."""
    return text, InlineKeyboardMarkup([_back()])


def build_settings(
    path: Path,
    values: Optional[Dict[str, Optional[int]]],
    error: Optional[str],
    undo: Optional[Dict[str, Any]],
    note: str = "",
) -> Tuple[str, InlineKeyboardMarkup]:
    """Настройки (dv-f6af): per Pump cap a full-width "name: value" row, then
    one row of two short buttons − +, so the phone never shortens a label
    (the old "➖ Pump параллельно" / "➕ …" pair looked identical). ↩ undoes
    the last change. ``values`` None (with ``error``) = edit by hand. bgjob
    slots are not shown: taskpump derives them (parallel + 1)."""
    lines = [note, ""] if note else []
    lines += ["🛠 Настройки", f"Pump ({path.name}), меняется {_NEXT_TICK}:"]
    rows: List[List[InlineKeyboardButton]] = []
    if values is None:
        lines.append(
            f"⚠ Не правлю: {error}. Правьте вручную, затем "
            "systemctl --user daemon-reload."
        )
    else:
        for name, s in pump_unit.EDITABLE.items():
            value = values.get(name)
            shown = f"{s.default} (по умолчанию)" if value is None else str(value)
            lines.append(f"• {s.label}: {shown}  ({s.lo}–{s.hi})")
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{s.label}: {s.default if value is None else value}",
                        callback_data="menu:settings",
                    )
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton("−", callback_data=f"menu:set:{name}:-"),
                    InlineKeyboardButton("+", callback_data=f"menu:set:{name}:+"),
                ]
            )
    lines += [_MEMORY_NOTE, "Модель этого чата: /model."]
    if undo is not None:
        label = pump_unit.EDITABLE[undo["name"]].label
        lines.append("↩ — отменить последнее изменение.")
        rows.append(
            [
                InlineKeyboardButton(
                    f"↩ {label}: {undo['new']}→{undo['old']}",
                    callback_data="menu:set:undo",
                )
            ]
        )
    rows.append(_back())
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_models(
    path: Path,
    routes: Any,
    error: Optional[str],
    undo: Optional[Dict[str, Any]],
    note: str = "",
) -> Tuple[str, InlineKeyboardMarkup]:
    """Модели: one button per job row (its harness / model), the jobs switch
    under the "Фоновые задачи на: …" top button, ↩ for the last change.
    ``routes`` None (with ``error``) = the file does not parse: by hand."""
    lines = [note, ""] if note else []
    lines += ["🧠 Модели стадий Pump", f"Файл: {path} — {_NEXT_STAGE}.", ""]
    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"Фоновые задачи на: {jobs_switch_name()} ▸",
                callback_data="menu:jobsw",
            )
        ]
    ]
    if routes is None:
        lines.append(f"⚠ Не правлю: {error}. Правьте вручную.")
    else:
        from ...model_router.types import JOB_ROUTE_NAMES

        for job in sorted(JOB_ROUTE_NAMES, key=_job_order):
            harness = job_rows.row_harness(routes, job)
            model = job_rows.row_model(routes, job) or "—"
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{job_rows.job_label(job)}: {harness} / {model}",
                        callback_data=f"menu:model:{job}",
                    )
                ]
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
    rows.append(_back())
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_model_choices(
    job: str, harness: str, current: Optional[str], choices: Tuple[str, ...]
) -> Tuple[str, InlineKeyboardMarkup]:
    """The models a stage may run on, for the row's harness."""
    rows: List[List[InlineKeyboardButton]] = []
    for model in choices:
        data = f"menu:mod:{job}:{model}"
        if len(data.encode("utf-8")) > 64:  # Telegram's callback_data cap
            continue
        mark = "✓ " if model == current else ""
        rows.append([InlineKeyboardButton(f"{mark}{model}", callback_data=data)])
    rows.append(_back("models"))
    return (
        f"🧠 {job_rows.job_label(job)} (jobs.{job}): {harness} / {current or '—'}\n"
        f"Выберите модель для {harness} — {_NEXT_STAGE}.",
        InlineKeyboardMarkup(rows),
    )


def _job_order(job: str) -> Tuple[int, str]:
    order = list(job_rows.JOB_LABELS)
    return (order.index(job) if job in order else len(order), job)


def _short(title: Any, limit: int = _TITLE_CHARS) -> str:
    text = str(title or "").strip("\"' ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _blocker(why: str) -> str:
    """The script's English hold reason in the owner's words."""
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
    if t.get("group") == "needs":
        return True
    hours = t.get("hours")
    return (
        t.get("group") == "review"
        and isinstance(hours, (int, float))
        and hours >= _REVIEW_WAIT_HOURS
    )


def _more(rows: List[Any], limit: int) -> str:
    return f"… ещё {len(rows) - limit}" if len(rows) > limit else ""


def build_pump(
    tasks: List[Dict[str, Any]],
    frozen: Optional[Dict[str, Any]],
    max_parallel: int,
) -> Tuple[str, InlineKeyboardMarkup]:
    """The Pump screen, from one ``status --json`` list: running N/max, the
    tasks waiting for the owner as buttons (→ the task's card), running and
    held tasks one line each, Freeze or Thaw (only here), Stop, « Назад."""
    by_group: Dict[str, List[Dict[str, Any]]] = {}
    for t in tasks:
        by_group.setdefault(str(t.get("group") or ""), []).append(t)
    running = by_group.get("running", [])
    waiting_you = [t for t in tasks if _is_waiting_for_owner(t)]
    held = by_group.get("waiting", []) + by_group.get("stopped", [])
    queued = by_group.get("queued", [])

    if frozen is None:
        head = f"⚙ Pump · работает {len(running)}/{max_parallel}"
    else:
        head = f"❄ Pump заморожен ({frozen.get('by') or '?'}) · работает {len(running)}/{max_parallel}"
    lines = [head]
    rows: List[List[InlineKeyboardButton]] = []
    if waiting_you:
        lines += ["", f"Ждут тебя ({len(waiting_you)}):"]
        for t in waiting_you[:_WAIT_BUTTONS]:
            tid = str(t["id"])
            icon = str(t.get("icon") or "") or _group_icon(t)
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{icon} {tid} {_short(t.get('title'), _BUTTON_TITLE_CHARS)}",
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


def _group_icon(t: Dict[str, Any]) -> str:
    return "👀" if t.get("group") == "review" else "❓"


def build_task_card(
    task: Optional[Dict[str, Any]], task_id: str
) -> Tuple[str, InlineKeyboardMarkup]:
    """One task: its state, and what can be done with it. ⚡ / 🚨 unless done or blocked;
    "всё по умолчанию" on a task with questions, "Ещё 5" on a capped one (both
    are ``taskpump answer <id> go``), "Повторить" on a failed one."""
    back = [_back("pump")]
    if task is None:
        return (
            f"{task_id}: не нахожу в Pump (уже ушла из конвейера?).",
            InlineKeyboardMarkup(back),
        )
    lines = [f"{task_id} — {_short(task.get('title'), 80)}"]
    state = str(task.get("why") or task.get("pump") or "")
    group = str(task.get("group") or "")
    if group == "review":
        lines.append(f"Готова, ждёт твоей проверки {int(task.get('hours') or 0)} ч.")
    else:
        lines.append(f"Состояние: {_blocker(state)}{task.get('act') or ''}")
    rows: List[List[InlineKeyboardButton]] = []
    pump = task.get("pump")
    if pump == "asked":
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
    elif pump == "capped":
        rows.append(
            [
                InlineKeyboardButton(
                    "▶ Ещё 5", callback_data=f"{MENU_PREFIX}go:{task_id}"
                )
            ]
        )
    elif pump == "error":
        rows.append(
            [
                InlineKeyboardButton(
                    "🔁 Повторить", callback_data=f"{MENU_PREFIX}retry:{task_id}"
                )
            ]
        )
    # ⚡ / 🚨 only where the pump would run the task: a finished (review)
    # card would be rebuilt by 🚨, and cmd_urgent refuses a blocked one.
    if group != "review" and pump not in ("done", "blocked"):
        rows.append(
            [
                InlineKeyboardButton(
                    "⚡ Первой", callback_data=f"{MENU_PREFIX}first:{task_id}"
                ),
                InlineKeyboardButton(
                    "🚨 Срочно", callback_data=f"{MENU_PREFIX}urgent:{task_id}"
                ),
            ]
        )
    rows.append(_back("pump"))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


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
            text, _ = await limits_panel(context)
            await self._menu_edit(query, *build_spend(text))
        elif action == "pump":
            await self._menu_pump(query)
        elif action == "settings":
            await self._menu_settings(query, context)
        elif action == "set" and arg:
            await self._menu_set(query, context, arg)
        elif action == "models":
            await self._menu_models(query, context)
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
            await self._menu_task(query, arg)
        elif action == "go" and arg:
            await self._menu_taskpump(query, "answer", arg, "go")
        elif action == "retry" and arg:
            await self._menu_taskpump(query, "retry", arg)
        elif action == "first" and arg:
            await self._menu_taskpump(
                query, "first", arg, "--why", _WHY_FIRST, "--by", "owner"
            )
        elif action == "urgent" and arg:
            await self._menu_edit(query, *build_urgent_ask(arg))
        elif action == "urgentgo" and arg:
            await self._menu_taskpump(query, "urgent", arg, "--by", "owner")
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
        """The one ``taskpump status --json`` call; None if it failed."""
        from ...claude.agent_tools import run_repo_script

        rc, out = await run_repo_script("taskpump", "status", "--json")
        return _status_rows(out) if rc == 0 else None

    async def _menu_pump(self, query: Any) -> None:
        rows = await self._status_rows()
        if rows is None:
            await self._menu_edit(
                query,
                "⚙ Pump\n\ntaskpump status не ответил — попробуй ещё раз.",
                InlineKeyboardMarkup([_back()]),
            )
            return
        try:
            values = pump_unit.effective(pump_unit.read_values(pump_unit.unit_path()))
            max_parallel = values[pump_unit.PUMP]
        except pump_unit.UnitError:
            max_parallel = pump_unit.EDITABLE[pump_unit.PUMP].default
        await self._menu_edit(query, *build_pump(rows, _pump_frozen(), max_parallel))

    async def _menu_task(self, query: Any, task_id: str) -> None:
        rows = await self._status_rows() or []
        task = next((t for t in rows if str(t.get("id")) == task_id), None)
        await self._menu_edit(query, *build_task_card(task, task_id))

    async def _menu_taskpump(self, query: Any, *argv: str) -> None:
        """Run one taskpump write and show its output, « Назад to Pump."""
        from ...claude.agent_tools import run_repo_script

        rc, out = await run_repo_script("taskpump", *argv)
        logger.info("menu: taskpump", argv=list(argv), rc=rc)
        head = "✅" if rc == 0 else f"❌ taskpump {argv[0]} failed (exit {rc})."
        await self._menu_edit(
            query,
            f"{head}\n{out[-800:]}".strip(),
            InlineKeyboardMarkup([_back("pump")]),
        )

    # --- Настройки: Pump caps in the taskpump unit -----------------------------

    async def _menu_settings(
        self, query: Any, context: ContextTypes.DEFAULT_TYPE, note: str = ""
    ) -> None:
        path = pump_unit.unit_path()
        values: Optional[Dict[str, Optional[int]]] = None
        error: Optional[str] = None
        undo = None
        try:
            values = pump_unit.read_values(path)
        except pump_unit.UnitError as exc:
            error = str(exc)
        if values is not None:
            event = await _last_change(
                context, query.from_user.id, str(path), pump_unit.EDITABLE
            )
            undo = event.event_data if event is not None else None
        await self._menu_edit(query, *build_settings(path, values, error, undo, note))

    async def _menu_set(
        self, query: Any, context: ContextTypes.DEFAULT_TYPE, arg: str
    ) -> None:
        """One −/+ step or ↩ undo: re-read the unit, check, write one line,
        daemon-reload, log old→new. A refusal writes nothing."""
        path = pump_unit.unit_path()
        user_id = query.from_user.id
        try:
            values = pump_unit.read_values(path)
            current = pump_unit.effective(values)
            undo_of = None
            if arg == "undo":
                event = await _last_change(
                    context, user_id, str(path), pump_unit.EDITABLE
                )
                if event is None:
                    await self._menu_settings(query, context, "Нечего отменять.")
                    return
                data = event.event_data
                name = data["name"]
                if str(current[name]) != str(data["new"]):
                    await self._menu_settings(
                        query,
                        context,
                        f"⚠ {name} уже {current[name]}, не {data['new']} "
                        "(правили после) — отмену не делаю.",
                    )
                    return
                new = int(data["old"])
                write: Optional[int] = None if data.get("unset") else new
                undo_of = event.id
            else:
                name, _, sign = arg.rpartition(":")
                if name not in pump_unit.EDITABLE or sign not in ("+", "-"):
                    await self._menu_settings(query, context, "Неизвестная кнопка.")
                    return
                new = current[name] + (1 if sign == "+" else -1)
                write = new
            reason = pump_unit.check(name, new, values)
            if reason:
                await self._menu_settings(query, context, f"⛔ {reason}")
                return
            pump_unit.write_value(path, name, write)
        except pump_unit.UnitError as exc:
            logger.warning("menu: unit not editable", path=str(path), error=str(exc))
            await self._menu_settings(query, context)
            return

        old = current[name]
        record: Dict[str, Any] = {
            "name": name,
            "old": old,
            "new": new,
            "source": str(path),
            "unset": values[name] is None,
        }
        if undo_of is not None:
            record["undo"] = undo_of
        logger.info("menu: admin setting", user_id=user_id, **record)
        await _audit(context, user_id, record)
        note = f"✅ {pump_unit.EDITABLE[name].label}: {old} → {new}, {_NEXT_TICK}."
        failed = await _daemon_reload()
        if failed:
            note += (
                f"\n⚠ daemon-reload не прошёл ({failed}): значение в файле, "
                "применится после systemctl --user daemon-reload."
            )
        await self._menu_settings(query, context, note)

    # --- Настройки → Модели: the jobs: rows of models.yaml ---------------------

    @staticmethod
    def _routes_path(context: ContextTypes.DEFAULT_TYPE) -> Path:
        settings = context.bot_data.get("settings")
        return job_rows.routes_path(getattr(settings, "model_routes_path", None))

    async def _menu_models(
        self, query: Any, context: ContextTypes.DEFAULT_TYPE, note: str = ""
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
        await self._menu_edit(query, *build_models(path, routes, error, undo, note))

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
                        query, context, f"{job_rows.job_label(job)}: уже {model}."
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


async def _daemon_reload() -> Optional[str]:
    """``systemctl --user daemon-reload`` so the next tick reads the unit;
    the error text, or None."""
    env = dict(os.environ)
    # systemd --user needs this; the bot process does not have it set.
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "daemon-reload",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except Exception as exc:
        return str(exc) or type(exc).__name__
    if proc.returncode:
        text = (out or b"").decode("utf-8", errors="replace").strip()
        return f"exit {proc.returncode}: {text[-200:]}"
    return None
