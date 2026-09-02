"""Session watchdog: admin alert + hold buttons (dv-8bb5).

When a user's session crosses one of the hourly thresholds in
``RateLimiter.record_watchdog_turn``, this sends the owner chat(s) a Telegram
message with three inline buttons -- hold 10 min, hold 1 day, dismiss -- and
returns immediately. Modeled on ``ConfirmationManager``
(``src/claude/confirmation.py``), minus the blocking ``asyncio.Future``: that
class gates an in-flight action and something is always awaiting the answer;
this alert is fire-and-forget about a turn that has already finished, and the
button press is a side effect (apply a hold) with nothing waiting on it.
"""

from __future__ import annotations

import uuid
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger()

# Telegram callback_data is capped at 64 bytes: "watchdog:<hex32>:10m" fits.
CALLBACK_PREFIX = "watchdog"

_METRIC_LABELS = {
    "cost_per_hour": ("💰 Cost/hour", "${:.2f}", "${:.2f}"),
    "messages_per_hour": ("💬 Messages/hour", "{:.0f}", "{:.0f}"),
    "turns_per_hour": ("🔁 Turns/hour", "{:.0f}", "{:.0f}"),
}


class WatchdogManager:
    """Owner alert + hold-button gate backed by an in-memory token map."""

    def __init__(
        self,
        send_alert: Callable[[int, str, str], Awaitable[Optional[int]]],
        chat_ids: Optional[List[int]],
        edit_alert: Optional[Callable[[int, int, str], Awaitable[None]]] = None,
    ) -> None:
        # send_alert(chat_id, text, token) -> sends a message with three
        # inline buttons whose callback_data is
        # f"{CALLBACK_PREFIX}:{token}:10m|1d|ack", returns the sent message's
        # id (or None if unavailable).
        self._send_alert = send_alert
        self._edit_alert = edit_alert
        self._chat_ids = list(chat_ids or [])
        # token -> user_id. No Future here: nothing blocks on the answer.
        self._pending: Dict[str, int] = {}
        self._pending_messages: Dict[str, Tuple[int, int]] = {}

    async def trigger(
        self, user_id: int, metric: str, value: float, threshold: float
    ) -> None:
        """Send the alert. Returns as soon as the message is sent (or dropped)."""
        if not self._chat_ids:
            logger.warning(
                "Watchdog alert dropped: no owner chat configured",
                user_id=user_id,
                metric=metric,
            )
            return

        token = uuid.uuid4().hex
        self._pending[token] = user_id

        label, value_fmt, threshold_fmt = _METRIC_LABELS.get(
            metric, (metric, "{:.2f}", "{:.2f}")
        )
        text = (
            "🐕 <b>Session watchdog</b>\n"
            f"User <code>{user_id}</code> crossed a threshold:\n"
            f"<b>{label}</b>: {value_fmt.format(value)} "
            f"(limit {threshold_fmt.format(threshold)})"
        )
        chat_id = self._chat_ids[0]
        try:
            message_id = await self._send_alert(chat_id, text, token)
        except Exception as exc:
            logger.warning("Failed to send watchdog alert", error=str(exc))
            self._pending.pop(token, None)
            return
        if message_id is not None:
            self._pending_messages[token] = (chat_id, message_id)
        logger.info("Watchdog alert sent", user_id=user_id, metric=metric, value=value)

    def resolve_user(self, token: str) -> Optional[int]:
        """The user an unresolved alert token is about, or None if the token
        is unknown (already actioned, or never issued)."""
        return self._pending.get(token)

    def clear(self, token: str) -> None:
        """Mark an alert resolved (called by the callback handler)."""
        self._pending.pop(token, None)
        self._pending_messages.pop(token, None)
