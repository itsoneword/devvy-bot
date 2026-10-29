"""Concrete event types for the event bus."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .bus import Event


@dataclass
class UserMessageEvent(Event):
    """A message from a Telegram user."""

    user_id: int = 0
    chat_id: int = 0
    text: str = ""
    working_directory: Path = field(default_factory=lambda: Path("."))
    source: str = "telegram"


@dataclass
class WebhookEvent(Event):
    """An external webhook delivery (GitHub, Notion, etc.)."""

    provider: str = ""
    event_type_name: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    delivery_id: str = ""
    source: str = "webhook"


@dataclass
class ScheduledEvent(Event):
    """A cron/scheduled trigger."""

    job_id: str = ""
    job_name: str = ""
    prompt: str = ""
    working_directory: Path = field(default_factory=lambda: Path("."))
    target_chat_ids: List[int] = field(default_factory=list)
    skill_name: Optional[str] = None
    source: str = "scheduler"
    # 'agent' = run an LLM turn with `prompt`; 'message' = deliver `prompt`
    # to Telegram verbatim, no LLM turn (one-shot reminders).
    delivery: str = "agent"
    # Telegram user id of whoever registered the job, 0 = unknown (webhook/
    # agentctl callers with no Telegram identity). The handler resolves the
    # job's real owner from this before target_chat_ids (dv-3e91) — without
    # it a job owned by one family member ran as another.
    created_by: int = 0
    # dv-752a: the ``items`` row this job fires, or None for a plain job.
    # The handler reads the live item and runs the job as its ASSIGNEE.
    item_id: Optional[int] = None
    # Explicit run-as user, set by the handler for item jobs; 0 = resolve
    # from created_by / target_chat_ids as before.
    run_as: int = 0


@dataclass
class AgentResponseEvent(Event):
    """An agent has produced a response to deliver."""

    chat_id: int = 0
    text: str = ""
    parse_mode: Optional[str] = "HTML"
    reply_to_message_id: Optional[int] = None
    source: str = "agent"
    originating_event_id: Optional[str] = None
    # When set, this is a live STATUS post rather than a new message: the
    # delivery layer edits (and keeps pinned) the single message registered
    # under this key instead of appending to the chat. Empty text means "the
    # status is over" -> unpin and forget. Used by the background-job status
    # pin so a long job's state is visible without scrolling.
    pin_as: Optional[str] = None
    # With pin_as: once per UTC day send the status as a NEW message at the
    # bottom, pin it, unpin the old one (dv-6646). Later pushes the same day
    # edit in place as usual.
    repin: bool = False
    # When ledger_kind is set, the delivery layer records each sent Telegram
    # message in the sent_messages ledger under this kind (e.g.
    # 'scheduled_reminder'), so a later reply to it can be attributed and
    # routed. profile_slug / messages_message_id fill the ledger columns.
    ledger_kind: Optional[str] = None
    ledger_profile_slug: Optional[str] = None
    ledger_messages_message_id: Optional[int] = None
    # What the message is about ("job:<id>", dv-c41e). Unlike ledger_kind it
    # does not change routing; it only tags the ledger row.
    ledger_ref: Optional[str] = None
    # Validated image/file paths sent after the text (dv-f90c); the internal
    # notify API checks them with validate_image_path before setting this.
    image_paths: List[str] = field(default_factory=list)
    # Deliver to this profile's own chat (dv-c41e amendment, 2026-10-22): its
    # bound topic, else the DM. Used by the batch job digest, which must not
    # land in Jobs (the log).
    deliver_profile: Optional[str] = None
