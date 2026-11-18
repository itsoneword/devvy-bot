"""/limits as a widget in the owner's DM (dv-4665, pilot of Bot API 10.4).

``/limits`` in a private chat sends one widget (no text message beside it):
bars per Claude / Codex window, reset times, limit and Pump-frozen markers,
"updated at". A Refresh tap sends the request ``r``; the bot answers with
fresh numbers. Nothing is written: no quota record, DB or Pump change.
Anywhere else, with ``LIMITS_WIDGET_ENABLED=false`` or on any failure the
caller sends the text panel.

The numbers come from the text panel's sources (``quota.read_usage()``,
``quota.active()``, the Codex backend's ``read_account_limits`` via
``codex_limits_reader``, ``_pump_frozen``). Telegram calls go through
``src/bot/telegram_widgets.py`` only.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ...security.auth import ROLE_OWNER, fallback_role
from ..orchestration.commands.info import (
    _pump_frozen,
    codex_limits_reader,
    codex_matters,
)
from ..telegram_widgets import (
    DATA_MAX_BYTES,
    WidgetQuery,
    answer_widget_query,
    send_widget,
)

logger = structlog.get_logger()

REFRESH = "r"  # the widget's only request
CODEX_TIMEOUT_S = 8.0  # a widget query must be answered within 15 s
ANSWER_DEADLINE_S = 12.0
MAX_WINDOWS = 3
WIDGET_HEIGHT = 190

_CLAUDE_LABELS = {
    "five_hour": "5ч",
    "seven_day": "7д",
    "seven_day_opus": "7д Opus",
    "seven_day_sonnet": "7д Sonnet",
}


def _epoch(at: Optional[datetime]) -> Optional[int]:
    return int(at.timestamp()) if at is not None else None


def _claude_part(now: datetime) -> Dict[str, Any]:
    from ...model_router import quota

    part: Dict[str, Any] = {"n": "Claude"}
    usage = quota.read_usage()
    if usage is not None:
        part["a"] = _epoch(usage.at)
        rows: List[List[Any]] = []
        for key, win in list(usage.windows.items())[:MAX_WINDOWS]:
            stale = win.resets_at is not None and win.resets_at <= now
            pct = (
                None
                if stale or win.utilization is None
                else round(win.utilization * 100)
            )
            rows.append([_CLAUDE_LABELS.get(key, key[:12]), pct, _epoch(win.resets_at)])
        part["w"] = rows
    else:
        part["e"] = "нет данных"
    limit = quota.active(now)
    if limit is not None:
        part["l"] = _epoch(limit.until)
    return part


def _duration(minutes: Any) -> str:
    if not isinstance(minutes, int) or minutes <= 0:
        return "?"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}д"
    if minutes % 60 == 0:
        return f"{minutes // 60}ч"
    return f"{minutes}м"


def _codex_rows(snapshot: Dict[str, Any]) -> List[List[Any]]:
    """Windows from the account snapshot (the shape ``format_codex_limits``
    reads)."""
    payload = snapshot.get("rate_limits") or {}
    limits = payload.get("rateLimitsByLimitId") or {}
    if not limits and isinstance(payload.get("rateLimits"), dict):
        one = payload["rateLimits"]
        limits = {one.get("limitId") or "codex": one}
    rows: List[List[Any]] = []
    for item in limits.values():
        if not isinstance(item, dict):
            continue
        for key in ("primary", "secondary"):
            window = item.get(key)
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            reset = window.get("resetsAt")
            rows.append(
                [
                    _duration(window.get("windowDurationMins")),
                    round(used) if isinstance(used, (int, float)) else None,
                    int(reset) if isinstance(reset, (int, float)) else None,
                ]
            )
    return rows[:MAX_WINDOWS]


async def _codex_part(bot_data: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    from ...model_router import quota

    part: Dict[str, Any] = {"n": "Codex"}
    limit = quota.active(now, provider="openai")
    if limit is not None:
        part["l"] = _epoch(limit.until)
    reader = codex_limits_reader(bot_data)
    if reader is None:
        part["e"] = "не подключён"
        return part
    db = getattr(bot_data.get("storage"), "db_manager", None)
    if not await codex_matters(db, limit):
        part["e"] = "не используется (7 дн.)"
        return part
    try:
        snapshot = await asyncio.wait_for(reader(), timeout=CODEX_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("limits widget: Codex account query timed out")
        part["e"] = "n/a"
        return part
    except Exception:
        logger.warning("limits widget: Codex account query failed", exc_info=True)
        part["e"] = "n/a"
        return part
    part["w"] = _codex_rows(snapshot if isinstance(snapshot, dict) else {})
    return part


def _dump(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def fit(payload: Dict[str, Any]) -> str:
    """The payload as compact JSON within 1024 bytes: rows dropped from the
    end until it fits (it fits by far at today's ≤ 6 rows)."""
    text = _dump(payload)
    parts = payload.get("p") or []
    while len(text.encode("utf-8")) > DATA_MAX_BYTES:
        longest = max(parts, key=lambda p: len(p.get("w") or []), default=None)
        if longest is None or not longest.get("w"):
            return _dump({"t": payload.get("t"), "e": "слишком много данных"})
        longest["w"].pop()
        text = _dump(payload)
    return text


async def build_payload(bot_data: Dict[str, Any]) -> Dict[str, Any]:
    """Fresh limits, read only (no write anywhere)."""
    now = datetime.now(UTC)
    payload: Dict[str, Any] = {
        "t": _epoch(now),
        "p": [_claude_part(now), await _codex_part(bot_data, now)],
    }
    try:
        if _pump_frozen() is not None:
            payload["f"] = 1
    except Exception:
        logger.warning("limits widget: Pump state unreadable", exc_info=True)
    return payload


def _clock(epoch: int, now: datetime) -> str:
    at = datetime.fromtimestamp(epoch, UTC)
    return at.strftime("%H:%M" if at.date() == now.date() else "%d.%m %H:%M")


def caption(payload: Dict[str, Any]) -> str:
    """The caption: the same numbers as plain text, for the chat list and for
    apps without widgets."""
    now = datetime.now(UTC)
    lines = [f"📊 Лимиты на {_clock(payload['t'], now)} UTC"]
    for part in payload.get("p") or []:
        rows = []
        for label, pct, reset in part.get("w") or []:
            row = f"{label} {pct if pct is not None else '?'}%"
            if reset:
                row += f" · сброс {_clock(reset, now)}"
            rows.append(row)
        body = "; ".join(rows) or part.get("e") or "нет данных"
        lines.append(f"{part['n']}: {body}")
        if part.get("l"):
            lines.append(f"⛔ {part['n']}: лимит до {_clock(part['l'], now)} UTC")
    if payload.get("f"):
        lines.append("Pump: заморожен")
    return "\n".join(lines)


# Theme variables only, no network, no external asset. Times in the viewer's
# local time; the caption has them in UTC.
WIDGET_HTML = """<!doctype html><meta charset="utf-8"><style>
body{margin:0;padding:10px 12px;font:0.9em system-ui,sans-serif;color:var(--tg-theme-text-color)}
.h{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.n{font-weight:600;margin-top:6px}.m{color:var(--tg-theme-hint-color);font-size:.85em}
.r{display:flex;align-items:center;gap:6px;margin:3px 0}.l{width:5.5em}
.b{flex:1;height:6px;border-radius:3px;background:var(--tg-theme-secondary-bg-color)}
.f{height:6px;border-radius:3px;background:var(--tg-theme-button-color)}
.x{color:var(--tg-theme-destructive-text-color)}
button{border:0;border-radius:8px;padding:4px 10px;font:inherit;
background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color)}
</style><div class="h"><b>Лимиты</b><button id="g">Обновить</button></div>
<div id="v"></div><div class="m" id="s"></div><script>
const W=window.TelegramWidget,V=document.getElementById('v'),S=document.getElementById('s');
function t(e){if(!e)return'';const d=new Date(e*1000),n=new Date(),
h=d.toTimeString().slice(0,5);return d.toDateString()==n.toDateString()?h:
d.getDate()+'.'+(d.getMonth()+1)+' '+h}
function el(c,x){const e=document.createElement('div');e.className=c;if(x)e.textContent=x;return e}
function draw(p){V.textContent='';if(!p)return;if(p.e&&!p.p){S.textContent=p.e;return}
for(const q of p.p||[]){V.appendChild(el('n',q.n));
if(q.l)V.appendChild(el('x','⛔ лимит до '+t(q.l)));
for(const w of q.w||[]){const r=el('r'),b=el('b'),f=el('f');
f.style.width=Math.min(100,w[1]||0)+'%';b.appendChild(f);
r.appendChild(el('l',w[0]));r.appendChild(b);
r.appendChild(el('m',(w[1]==null?'?':w[1])+'%'+(w[2]?' · '+t(w[2]):'')));V.appendChild(r)}
if(q.e)V.appendChild(el('m',q.e))}
if(p.f)V.appendChild(el('x','Pump: заморожен'));S.textContent='обновлено '+t(p.t)}
function parse(s){try{return JSON.parse(s)}catch(e){return null}}
draw(parse(W.chatData));W.onEvent('chatDataChanged',()=>draw(parse(W.chatData)));
document.getElementById('g').addEventListener('click',()=>{S.textContent='обновляю…';
W.request('r').then(a=>{const p=parse(a.result);if(p)draw(p);else S.textContent='нет ответа'},
e=>{S.textContent='ошибка: '+e.code})});
if(W.ready)W.ready();
</script>"""


def is_owner(user_id: Optional[int], bot_data: Dict[str, Any]) -> bool:
    """``from.id`` is authenticated by Telegram; the role comes from the auth
    manager's live session, else the env owners (no auth write here)."""
    if user_id is None:
        return False
    auth = bot_data.get("auth_manager")
    role = None
    if auth is not None:
        try:
            role = auth.get_user_role(user_id)
        except Exception:
            role = None
    if not isinstance(role, str):
        role = fallback_role(user_id, bot_data.get("settings"))
    return role == ROLE_OWNER


async def send_limits_widget(
    update: Update, context: ContextTypes.DEFAULT_TYPE, settings: Any
) -> bool:
    """Send the widget for /limits in a private chat. False means "send the
    text": not a private chat, switched off, or the send failed (logged)."""
    message = update.message
    if message is None or not getattr(settings, "limits_widget_enabled", False):
        return False
    if getattr(message.chat, "type", None) != "private":
        return False
    try:
        payload = await build_payload(context.bot_data)
        thread = message.message_thread_id
        await send_widget(
            context.bot,
            message.chat.id,
            WIDGET_HTML,
            caption=caption(payload),
            chat_data=fit(payload),
            allow_queries=True,
            message_thread_id=thread if isinstance(thread, int) else None,
            height=WIDGET_HEIGHT,
        )
    except Exception as exc:
        logger.warning(
            "limits widget: send failed, text fallback",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False
    return True


async def handle_widget_query(
    query: WidgetQuery, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Answer one widget query, always exactly once (Telegram needs an
    answer to every query within 15 s). Only the owner gets numbers."""
    bot_data = context.bot_data
    if not is_owner(query.from_user_id, bot_data):
        data = _dump({"e": "только для владельца"})
    elif query.data != REFRESH:
        data = _dump({"e": "неизвестный запрос"})
    else:
        try:
            payload = await asyncio.wait_for(
                build_payload(bot_data), timeout=ANSWER_DEADLINE_S
            )
            data = fit(payload)
        except Exception:
            logger.warning("limits widget: refresh failed", exc_info=True)
            data = _dump({"e": "не удалось прочитать лимиты"})
    try:
        await answer_widget_query(context.bot, query.id, data)
    except Exception as exc:
        logger.warning(
            "limits widget: answer failed", error=str(exc), query_id=query.id
        )
