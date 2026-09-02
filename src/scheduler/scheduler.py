"""Job scheduler for recurring agent tasks.

Wraps APScheduler's AsyncIOScheduler and publishes ScheduledEvents
to the event bus when jobs fire.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from apscheduler.schedulers.asyncio import (
    AsyncIOScheduler,  # type: ignore[import-untyped]
)
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.date import DateTrigger  # type: ignore[import-untyped]

from ..events.bus import EventBus
from ..events.types import ScheduledEvent
from ..storage.database import DatabaseManager

logger = structlog.get_logger()

# crontab numbers days 0=Sunday..6=Saturday (7 also Sunday); APScheduler numbers
# them 0=Monday..6=Sunday and `CronTrigger.from_crontab` does NOT translate —
# it passes the digits straight through. Every numeric-DOW job therefore fired
# one day late (observed: `0 19 * * 4` intended Thursday, fired Friday). Named
# days ("thu", "mon-fri") are parsed correctly by APScheduler and pass through.
_CRONTAB_TO_APS_DOW = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6}


def _translate_dow_token(token: str) -> str:
    token = token.strip()
    if token.isdigit() and int(token) in _CRONTAB_TO_APS_DOW:
        return str(_CRONTAB_TO_APS_DOW[int(token)])
    return token


def _translate_dow_field(field: str) -> str:
    """Rewrite a crontab day-of-week field into APScheduler numbering."""
    parts: List[str] = []
    for raw in field.split(","):
        part, sep, step = raw.strip().partition("/")
        step = f"{sep}{step}" if sep else ""
        if "-" in part:
            start, _, end = part.partition("-")
            new_start, new_end = _translate_dow_token(start), _translate_dow_token(end)
            if (
                new_start.isdigit()
                and new_end.isdigit()
                and int(new_start) > int(new_end)
            ):
                # Range wrapped around the week boundary after remapping
                # (e.g. crontab 0-4 = Sun-Thu -> 6-3). Split it in two.
                if step:
                    logger.warning(
                        "Cannot remap stepped wrapping day-of-week range; "
                        "leaving as-is",
                        field=field,
                    )
                    parts.append(raw.strip())
                    continue
                parts.extend([f"{new_start}-6", f"0-{new_end}"])
                continue
            parts.append(f"{new_start}-{new_end}{step}")
            continue
        parts.append(f"{_translate_dow_token(part)}{step}")
    return ",".join(parts)


def build_cron_trigger(cron_expression: str) -> CronTrigger:
    """Build a CronTrigger from a standard 5-field crontab expression.

    Use this instead of ``CronTrigger.from_crontab`` — see the note on
    ``_CRONTAB_TO_APS_DOW`` for why.
    """
    fields = cron_expression.split()
    if len(fields) == 5:
        translated = _translate_dow_field(fields[4])
        if translated != fields[4]:
            logger.debug(
                "Translated crontab day-of-week to APScheduler numbering",
                cron=cron_expression,
                dow_in=fields[4],
                dow_out=translated,
            )
        fields = fields[:4] + [translated]
    return CronTrigger.from_crontab(" ".join(fields))


class JobScheduler:
    """Cron scheduler that publishes ScheduledEvents to the event bus."""

    def __init__(
        self,
        event_bus: EventBus,
        db_manager: DatabaseManager,
        default_working_directory: Path,
    ) -> None:
        self.event_bus = event_bus
        self.db_manager = db_manager
        self.default_working_directory = default_working_directory
        self._scheduler = AsyncIOScheduler()

    async def start(self) -> None:
        """Load persisted jobs and start the scheduler."""
        await self._load_jobs_from_db()
        self._scheduler.start()
        logger.info("Job scheduler started")

    async def stop(self) -> None:
        """Shutdown the scheduler gracefully."""
        self._scheduler.shutdown(wait=False)
        logger.info("Job scheduler stopped")

    def pause(self) -> None:
        """Pause job processing (killswitch). Jobs stay registered; none fire."""
        if self._scheduler.running:
            self._scheduler.pause()
            logger.info("Job scheduler paused")

    def resume(self) -> None:
        """Resume job processing after a pause."""
        if self._scheduler.running:
            self._scheduler.resume()
            logger.info("Job scheduler resumed")

    async def add_job(
        self,
        job_name: str,
        cron_expression: Optional[str],
        prompt: str,
        target_chat_ids: Optional[List[int]] = None,
        working_directory: Optional[Path] = None,
        skill_name: Optional[str] = None,
        created_by: int = 0,
        run_at: Optional[datetime] = None,
        delivery: str = "agent",
    ) -> str:
        """Add a new scheduled job.

        Args:
            job_name: Human-readable job name.
            cron_expression: Cron-style schedule (e.g. "0 9 * * 1-5"),
                or None for a one-shot job.
            prompt: The prompt to send to Claude when the job fires.
            target_chat_ids: Telegram chat IDs to send the response to.
            working_directory: Working directory for Claude execution.
            skill_name: Optional skill to invoke.
            created_by: Telegram user ID of the creator.
            run_at: Fire once at this time instead of on a cron schedule.
                Exactly one of cron_expression / run_at must be given.
            delivery: 'agent' runs an LLM turn with the prompt (default);
                'message' delivers the prompt text to Telegram verbatim.

        Returns:
            The job ID.
        """
        if bool(cron_expression) == bool(run_at):
            raise ValueError("Provide exactly one of cron_expression or run_at")
        if delivery not in ("agent", "message"):
            raise ValueError("delivery must be 'agent' or 'message'")

        if run_at is not None:
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=UTC)
            trigger = DateTrigger(run_date=run_at)
            # One-shot jobs are persisted with an @at: marker in place of cron
            stored_expression = f"@at:{run_at.isoformat()}"
        else:
            trigger = build_cron_trigger(cron_expression or "")
            stored_expression = cron_expression or ""
        work_dir = working_directory or self.default_working_directory

        import uuid

        job_id = uuid.uuid4().hex

        self._scheduler.add_job(
            self._fire_event,
            trigger=trigger,
            kwargs={
                "job_name": job_name,
                "prompt": prompt,
                "working_directory": str(work_dir),
                "target_chat_ids": target_chat_ids or [],
                "skill_name": skill_name,
                "job_db_id": job_id,
                "one_shot": run_at is not None,
                "delivery": delivery,
                "created_by": created_by,
            },
            id=job_id,
            name=job_name,
        )

        # Persist to database
        await self._save_job(
            job_id=job_id,
            job_name=job_name,
            cron_expression=stored_expression,
            prompt=prompt,
            target_chat_ids=target_chat_ids or [],
            working_directory=str(work_dir),
            skill_name=skill_name,
            created_by=created_by,
            delivery=delivery,
        )

        logger.info(
            "Scheduled job added",
            job_id=job_id,
            job_name=job_name,
            cron=stored_expression,
        )
        return job_id

    def add_native_job(
        self,
        name: str,
        cron_expression: str,
        coroutine: Any,
    ) -> None:
        """Register a plain async callable on the underlying scheduler.

        Unlike :meth:`add_job` this does NOT persist to ``scheduled_jobs``
        and does NOT route through an agent turn — it is for in-process
        maintenance jobs (e.g. the weekly memory consolidation pass, dv-4c6f)
        that need DB reads and a CLI call, not an agent with tools. The
        caller re-registers on every boot, so persistence would only create
        drift.
        """
        self._scheduler.add_job(
            coroutine,
            trigger=build_cron_trigger(cron_expression),
            id=f"native:{name}",
            name=name,
            replace_existing=True,
        )
        logger.info("Native job registered", name=name, cron=cron_expression)

    async def remove_job(self, job_id: str) -> bool:
        """Remove a scheduled job."""
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            logger.warning("Job not found in scheduler", job_id=job_id)

        await self._delete_job(job_id)
        logger.info("Scheduled job removed", job_id=job_id)
        return True

    async def list_jobs(self) -> List[Dict[str, Any]]:
        """List all scheduled jobs from the database."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM scheduled_jobs WHERE is_active = 1 ORDER BY created_at"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def _fire_event(
        self,
        job_name: str,
        prompt: str,
        working_directory: str,
        target_chat_ids: List[int],
        skill_name: Optional[str],
        job_db_id: str = "",
        one_shot: bool = False,
        delivery: str = "agent",
        created_by: int = 0,
    ) -> None:
        """Called by APScheduler when a job triggers. Publishes a ScheduledEvent."""
        event = ScheduledEvent(
            job_id=job_db_id,
            job_name=job_name,
            prompt=prompt,
            working_directory=Path(working_directory),
            target_chat_ids=target_chat_ids,
            skill_name=skill_name,
            delivery=delivery,
            created_by=created_by,
        )

        logger.info(
            "Scheduled job fired",
            job_name=job_name,
            event_id=event.id,
        )

        await self.event_bus.publish(event)

        # One-shot jobs are spent after firing; deactivate the DB row so
        # they are not re-registered on restart.
        if one_shot and job_db_id:
            await self._delete_job(job_db_id)

    async def _load_jobs_from_db(self) -> None:
        """Load persisted jobs and re-register them with APScheduler."""
        try:
            async with self.db_manager.get_connection() as conn:
                cursor = await conn.execute(
                    "SELECT * FROM scheduled_jobs WHERE is_active = 1"
                )
                rows = list(await cursor.fetchall())

            for row in rows:
                row_dict = dict(row)
                try:
                    expression = row_dict["cron_expression"]
                    one_shot = expression.startswith("@at:")
                    if one_shot:
                        run_at = datetime.fromisoformat(expression[4:])
                        if run_at <= datetime.now(UTC):
                            # Missed while the bot was down; drop it.
                            await self._delete_job(row_dict["job_id"])
                            logger.info(
                                "Dropped expired one-shot job",
                                job_id=row_dict["job_id"],
                                job_name=row_dict["job_name"],
                            )
                            continue
                        trigger: Any = DateTrigger(run_date=run_at)
                    else:
                        trigger = build_cron_trigger(expression)

                    # Parse target_chat_ids from stored string
                    chat_ids_str = row_dict.get("target_chat_ids", "")
                    chat_ids = (
                        [int(x) for x in chat_ids_str.split(",") if x.strip()]
                        if chat_ids_str
                        else []
                    )

                    self._scheduler.add_job(
                        self._fire_event,
                        trigger=trigger,
                        kwargs={
                            "job_name": row_dict["job_name"],
                            "prompt": row_dict["prompt"],
                            "working_directory": row_dict["working_directory"],
                            "target_chat_ids": chat_ids,
                            "skill_name": row_dict.get("skill_name"),
                            "job_db_id": row_dict["job_id"],
                            "one_shot": one_shot,
                            "delivery": row_dict.get("delivery") or "agent",
                            "created_by": row_dict.get("created_by") or 0,
                        },
                        id=row_dict["job_id"],
                        name=row_dict["job_name"],
                        replace_existing=True,
                    )
                    logger.debug(
                        "Loaded scheduled job from DB",
                        job_id=row_dict["job_id"],
                        job_name=row_dict["job_name"],
                    )
                except Exception:
                    logger.exception(
                        "Failed to load scheduled job",
                        job_id=row_dict.get("job_id"),
                    )

            logger.info("Loaded scheduled jobs from database", count=len(rows))
        except Exception:
            # Table might not exist yet on first run
            logger.debug("No scheduled_jobs table found, starting fresh")

    async def _save_job(
        self,
        job_id: str,
        job_name: str,
        cron_expression: str,
        prompt: str,
        target_chat_ids: List[int],
        working_directory: str,
        skill_name: Optional[str],
        created_by: int,
        delivery: str = "agent",
    ) -> None:
        """Persist a job definition to the database."""
        chat_ids_str = ",".join(str(cid) for cid in target_chat_ids)
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT OR REPLACE INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt, target_chat_ids,
                 working_directory, skill_name, created_by, is_active, delivery)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    job_id,
                    job_name,
                    cron_expression,
                    prompt,
                    chat_ids_str,
                    working_directory,
                    skill_name,
                    created_by,
                    delivery,
                ),
            )
            await conn.commit()

    async def _delete_job(self, job_id: str) -> None:
        """Soft-delete a job from the database."""
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                "UPDATE scheduled_jobs SET is_active = 0 WHERE job_id = ?",
                (job_id,),
            )
            await conn.commit()
