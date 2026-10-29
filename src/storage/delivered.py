"""Persist text the user was sent outside an agent's reply (dv-da50).

Three senders deliver text to a chat that no turn's ``_store_interaction``
records: the memory-system notices (``main.py``), verbatim scheduled
reminders (``AgentHandler``) and the agent's own ``notify`` tool. Each writes
it as a ``messages`` row in the user's session for that workspace, so the
next turn's history and ``recall_conversation`` see that the user was told.
This is the one implementation of that write.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional, Union

from .models import MessageModel


async def persist_delivered_text(
    storage: Any,
    window_repo: Any,
    user_id: int,
    working_dir: Union[str, Path],
    prompt: str,
    response: str,
) -> Optional[int]:
    """Save one messages row in ``user_id``'s session for ``working_dir``.

    Returns the message id, or None when the user has no conversation in that
    workspace yet (nothing for an agent to see). Stamps the current window
    when ``window_repo`` is given. Raises on storage errors — callers decide
    whether delivery should survive them.
    """
    sessions = await storage.sessions.get_sessions_by_project(str(working_dir))
    session = next((s for s in sessions if s.user_id == user_id), None)
    if session is None:
        return None
    window_id = None
    if window_repo is not None:
        state = await window_repo.get_or_create(user_id, Path(working_dir))
        window_id = state.window_id
    message_id: int = await storage.messages.save_message(
        MessageModel(
            session_id=session.session_id,
            user_id=user_id,
            timestamp=datetime.now(UTC),
            prompt=prompt,
            response=response,
            window_id=window_id,
        )
    )
    return message_id
