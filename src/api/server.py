"""FastAPI webhook server.

Runs in the same process as the bot, sharing the event loop.
Receives external webhooks and publishes them as events on the bus.

Also exposes /internal/* endpoints (Bearer-authed with WEBHOOK_API_SECRET)
that agents call via scripts/agentctl to send messages, schedule jobs,
and manage the shared task board.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import structlog
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from ..config.settings import Settings
from ..events.bus import EventBus
from ..events.types import AgentResponseEvent, ScheduledEvent, WebhookEvent
from ..storage.database import DatabaseManager
from ..storage.deviz_board import DevizBoard
from .auth import verify_github_signature, verify_shared_secret

if TYPE_CHECKING:
    from ..profiles.registry import ProfileRegistry
    from ..scheduler.scheduler import JobScheduler

logger = structlog.get_logger()


class NotifyRequest(BaseModel):
    text: str
    chat_id: Optional[int] = None
    # When set, edit/keep-pinned the single status post under this key instead
    # of appending a new message. Empty text retires the pin.
    pin_as: Optional[str] = None


class PromptRequest(BaseModel):
    """Run one agent turn in a profile's conversation, right now.

    The turn auto-resumes the profile's current session (sessions are keyed
    by user+working_dir), so the conversation itself learns what happened —
    used by bgjob to have the agent digest and report a finished job's output
    instead of the user getting only a raw file-tail ping.
    """

    prompt: str
    profile: str
    name: str = "prompt"
    chat_id: Optional[int] = None


class ScheduleRequest(BaseModel):
    name: str
    prompt: str
    cron: Optional[str] = None
    at: Optional[datetime] = None
    profile: Optional[str] = None
    target_chat_ids: Optional[List[int]] = None


class TaskCreateRequest(BaseModel):
    title: str
    body: Optional[str] = None
    created_by: str = "user"
    assignee: Optional[str] = None
    due_at: Optional[datetime] = None


class TaskUpdateRequest(BaseModel):
    status: str


def create_api_app(
    event_bus: EventBus,
    settings: Settings,
    db_manager: Optional[DatabaseManager] = None,
    scheduler: Optional["JobScheduler"] = None,
    profile_registry: Optional["ProfileRegistry"] = None,
) -> FastAPI:
    """Create the FastAPI application."""

    app = FastAPI(
        title="Claude Code Telegram - Webhook API",
        version="0.1.0",
        docs_url="/docs" if settings.development_mode else None,
        redoc_url=None,
    )

    @app.get("/health")
    async def health_check() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/{provider}")
    async def receive_webhook(
        provider: str,
        request: Request,
        x_hub_signature_256: Optional[str] = Header(None),
        x_github_event: Optional[str] = Header(None),
        x_github_delivery: Optional[str] = Header(None),
        authorization: Optional[str] = Header(None),
    ) -> Dict[str, str]:
        """Receive and validate webhook from an external provider."""
        body = await request.body()

        # Verify signature based on provider
        if provider == "github":
            secret = settings.github_webhook_secret
            if not secret:
                raise HTTPException(
                    status_code=500,
                    detail="GitHub webhook secret not configured",
                )
            if not verify_github_signature(body, x_hub_signature_256, secret):
                logger.warning(
                    "GitHub webhook signature verification failed",
                    delivery_id=x_github_delivery,
                )
                raise HTTPException(status_code=401, detail="Invalid signature")

            event_type_name = x_github_event or "unknown"
            delivery_id = x_github_delivery or str(uuid.uuid4())
        else:
            # Generic provider — require auth (fail-closed)
            secret = settings.webhook_api_secret
            if not secret:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "Webhook API secret not configured. "
                        "Set WEBHOOK_API_SECRET to accept "
                        "webhooks from this provider."
                    ),
                )
            if not verify_shared_secret(authorization, secret):
                raise HTTPException(status_code=401, detail="Invalid authorization")
            event_type_name = request.headers.get("X-Event-Type", "unknown")
            delivery_id = request.headers.get("X-Delivery-ID", str(uuid.uuid4()))

        # Parse JSON payload
        try:
            payload: Dict[str, Any] = await request.json()
        except Exception:
            payload = {"raw_body": body.decode("utf-8", errors="replace")[:5000]}

        # Atomic dedupe: attempt INSERT first, only publish if new
        if db_manager and delivery_id:
            is_new = await _try_record_webhook(
                db_manager,
                event_id=str(uuid.uuid4()),
                provider=provider,
                event_type=event_type_name,
                delivery_id=delivery_id,
                payload=payload,
            )
            if not is_new:
                logger.info(
                    "Duplicate webhook delivery ignored",
                    provider=provider,
                    delivery_id=delivery_id,
                )
                return {
                    "status": "duplicate",
                    "delivery_id": delivery_id,
                }

        # Publish event to the bus
        event = WebhookEvent(
            provider=provider,
            event_type_name=event_type_name,
            payload=payload,
            delivery_id=delivery_id,
        )

        await event_bus.publish(event)

        logger.info(
            "Webhook received and published",
            provider=provider,
            event_type=event_type_name,
            delivery_id=delivery_id,
            event_id=event.id,
        )

        return {"status": "accepted", "event_id": event.id}

    # --- Internal agent-facing endpoints (agentctl) ---

    # The board is files, not a table (dv-1327) — available even when no DB is
    # wired, unlike the agent_tasks repo it replaced.
    task_repo = DevizBoard(settings.deviz_tasks_dir)

    def _require_internal_auth(authorization: Optional[str]) -> None:
        secret = settings.webhook_api_secret
        if not secret:
            raise HTTPException(
                status_code=503,
                detail="WEBHOOK_API_SECRET not configured",
            )
        if not verify_shared_secret(authorization, secret):
            raise HTTPException(status_code=401, detail="Invalid authorization")

    def _resolve_working_dir(profile_slug: Optional[str]) -> Optional[Path]:
        if not profile_slug:
            return None
        if not profile_registry:
            raise HTTPException(status_code=400, detail="Profiles not configured")
        profile = profile_registry.get_by_slug(profile_slug)
        if not profile:
            raise HTTPException(
                status_code=404, detail=f"Unknown profile: {profile_slug}"
            )
        return profile.working_dir

    @app.post("/internal/notify")
    async def internal_notify(
        body: NotifyRequest,
        authorization: Optional[str] = Header(None),
    ) -> Dict[str, str]:
        """Send a Telegram message to the user right now."""
        _require_internal_auth(authorization)
        event = AgentResponseEvent(
            chat_id=body.chat_id or 0,
            text=body.text,
            parse_mode=None,
            pin_as=body.pin_as,
        )
        await event_bus.publish(event)
        logger.info("Internal notify published", chat_id=body.chat_id or 0)
        return {"status": "sent", "event_id": event.id}

    @app.post("/internal/prompt")
    async def internal_prompt(
        body: PromptRequest,
        authorization: Optional[str] = Header(None),
    ) -> Dict[str, str]:
        """Trigger an immediate agent turn for a profile (resumed session)."""
        _require_internal_auth(authorization)
        working_dir = _resolve_working_dir(body.profile)
        if working_dir is None:
            raise HTTPException(status_code=400, detail="profile is required")
        event = ScheduledEvent(
            job_id=f"prompt-{uuid.uuid4().hex[:8]}",
            job_name=body.name,
            prompt=body.prompt,
            working_directory=working_dir,
            target_chat_ids=[body.chat_id] if body.chat_id else [],
        )
        await event_bus.publish(event)
        logger.info("Internal prompt published", profile=body.profile, name=body.name)
        return {"status": "queued", "event_id": event.id}

    @app.post("/internal/schedule")
    async def internal_schedule_add(
        body: ScheduleRequest,
        authorization: Optional[str] = Header(None),
    ) -> Dict[str, str]:
        """Register a cron or one-shot job (survives restarts)."""
        _require_internal_auth(authorization)
        if not scheduler:
            raise HTTPException(status_code=503, detail="Scheduler not enabled")
        if bool(body.cron) == bool(body.at):
            raise HTTPException(
                status_code=400, detail="Provide exactly one of cron / at"
            )
        at = body.at
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        if at is not None and at <= datetime.now(UTC):
            raise HTTPException(status_code=400, detail="'at' is in the past")
        working_dir = _resolve_working_dir(body.profile)
        if body.target_chat_ids and len(body.target_chat_ids) > 1 and profile_registry:
            profile = (
                profile_registry.get_by_slug(body.profile) if body.profile else None
            )
            # One turn can't be two people's private session — the in-process
            # tool already enforces a single target, this path had no such
            # check (dv-3e91 open question 4). Shared (non-personal_memory)
            # profiles keep allowing a broadcast to several chats.
            if profile is not None and profile.personal_memory:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "multi-target scheduling is not allowed on a "
                        "personal_memory profile"
                    ),
                )
        try:
            job_id = await scheduler.add_job(
                job_name=body.name,
                cron_expression=body.cron,
                prompt=body.prompt,
                target_chat_ids=body.target_chat_ids,
                working_directory=working_dir,
                run_at=at,
                # No Telegram identity on this path (webhook/agentctl
                # caller) — 0 is an honest value here, not a bug (dv-3e91).
                created_by=0,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"status": "scheduled", "job_id": job_id}

    @app.get("/internal/schedule")
    async def internal_schedule_list(
        authorization: Optional[str] = Header(None),
    ) -> List[Dict[str, Any]]:
        _require_internal_auth(authorization)
        if not scheduler:
            raise HTTPException(status_code=503, detail="Scheduler not enabled")
        return await scheduler.list_jobs()

    @app.delete("/internal/schedule/{job_id}")
    async def internal_schedule_remove(
        job_id: str,
        authorization: Optional[str] = Header(None),
    ) -> Dict[str, str]:
        _require_internal_auth(authorization)
        if not scheduler:
            raise HTTPException(status_code=503, detail="Scheduler not enabled")
        await scheduler.remove_job(job_id)
        return {"status": "removed", "job_id": job_id}

    @app.post("/internal/tasks")
    async def internal_task_add(
        body: TaskCreateRequest,
        authorization: Optional[str] = Header(None),
    ) -> Dict[str, Any]:
        _require_internal_auth(authorization)
        if not task_repo:
            raise HTTPException(status_code=503, detail="Storage not available")
        due_at = body.due_at
        if due_at is not None and due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=UTC)
        task_id = await task_repo.add_task(
            title=body.title,
            body=body.body,
            created_by=body.created_by,
            assignee=body.assignee,
            due_at=due_at,
        )
        return {"status": "created", "task_id": task_id}

    @app.get("/internal/tasks")
    async def internal_task_list(
        status: str = "open",
        authorization: Optional[str] = Header(None),
    ) -> List[Dict[str, Any]]:
        _require_internal_auth(authorization)
        if not task_repo:
            raise HTTPException(status_code=503, detail="Storage not available")
        tasks = await task_repo.list_tasks(status=None if status == "all" else status)
        return [t.to_dict() for t in tasks]

    @app.patch("/internal/tasks/{task_id}")
    async def internal_task_update(
        task_id: str,
        body: TaskUpdateRequest,
        authorization: Optional[str] = Header(None),
    ) -> Dict[str, Any]:
        _require_internal_auth(authorization)
        if not task_repo:
            raise HTTPException(status_code=503, detail="Storage not available")
        if body.status not in ("open", "done"):
            raise HTTPException(status_code=400, detail="status must be open|done")
        if not await task_repo.set_status(task_id, body.status):
            raise HTTPException(status_code=404, detail="Task not found")
        return {"status": "updated", "task_id": task_id}

    return app


async def _try_record_webhook(
    db_manager: DatabaseManager,
    event_id: str,
    provider: str,
    event_type: str,
    delivery_id: str,
    payload: Dict[str, Any],
) -> bool:
    """Atomically insert a webhook event, returning whether it was new.

    Uses INSERT OR IGNORE on the unique delivery_id column.
    If the row already exists the insert is a no-op and changes() == 0.
    Returns True if the event is new (inserted), False if duplicate.
    """
    import json

    async with db_manager.get_connection() as conn:
        await conn.execute(
            """
            INSERT OR IGNORE INTO webhook_events
            (event_id, provider, event_type, delivery_id, payload,
             processed)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (
                event_id,
                provider,
                event_type,
                delivery_id,
                json.dumps(payload),
            ),
        )
        cursor = await conn.execute("SELECT changes()")
        row = await cursor.fetchone()
        inserted = row[0] > 0 if row else False
        await conn.commit()
        return inserted


async def run_api_server(
    event_bus: EventBus,
    settings: Settings,
    db_manager: Optional[DatabaseManager] = None,
    scheduler: Optional["JobScheduler"] = None,
    profile_registry: Optional["ProfileRegistry"] = None,
) -> None:
    """Run the FastAPI server using uvicorn."""
    import uvicorn

    app = create_api_app(event_bus, settings, db_manager, scheduler, profile_registry)

    config = uvicorn.Config(
        app=app,
        host=settings.api_server_host,
        port=settings.api_server_port,
        log_level="info" if not settings.debug else "debug",
    )
    server = uvicorn.Server(config)
    await server.serve()
