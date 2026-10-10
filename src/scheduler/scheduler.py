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
from ..storage.scheduler_repository import SchedulerRepository

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


def _parse_chat_ids(value: Any) -> List[int]:
    """``target_chat_ids`` as stored: a comma-separated string, or empty."""
    if not value:
        return []
    return [int(x) for x in str(value).split(",") if str(x).strip()]


def job_visible_to(
    row: Dict[str, Any],
    user_id: Optional[int],
    role: Optional[str],
    requester_chat_id: Optional[int] = None,
) -> bool:
    """May this user see or manage this ``scheduled_jobs`` row? (dv-8c19)

    The single authorization rule for every read/write path on scheduled
    jobs — the in-process agent tools (``schedule_list``/``schedule_remove``
    in ``src/claude/agent_tools.py``, shared by both the Claude and Codex
    backends), and the ``/schedules`` command — so one path's denial rule
    cannot drift from another's. Neither `list_jobs()` nor `remove_job()`
    filter by owner themselves; the gate lives here, one level up, and every
    caller must apply it.

    Rules, fail-closed:

    * yours when you created it (``created_by`` matches ``user_id``);
    * yours when it TARGETS you — your chat id is among ``target_chat_ids``
      — covering a job one family member scheduled *for* another (e.g. the
      live ``friday-weighin-reminder``, created by the husband, delivered to
      the wife's chat: she must be able to see/cancel her own reminder);
    * an unattributed legacy row (``created_by == 0``, written before
      dv-3e91 taught ``schedule_add`` to stamp a creator) is visible to an
      ``owner``-role caller only.

    Deliberately NOT "owner sees everything": this deployment has two
    owner-role people, and the whole point of the ticket is that one must
    not read or cancel the other's personal jobs just because both are
    owners.
    """
    created_by = int(row.get("created_by") or 0)
    if created_by:
        if user_id is not None and created_by == int(user_id):
            return True
        if requester_chat_id is not None:
            if int(requester_chat_id) in _parse_chat_ids(row.get("target_chat_ids")):
                return True
        return False
    return role == "owner"


def build_cron_trigger(
    cron_expression: str,
    timezone: Optional[str] = None,
    end_date: Optional[datetime] = None,
) -> CronTrigger:
    """Build a CronTrigger from a standard 5-field crontab expression.

    Use this instead of ``CronTrigger.from_crontab`` — see the note on
    ``_CRONTAB_TO_APS_DOW`` for why.

    ``timezone`` (IANA name) makes the fields local wall-clock time: without
    it the scheduler runs UTC and a "17:00" routine drifts an hour at every
    DST change (dv-752a). None keeps the historical UTC behaviour. An
    unknown zone name raises ``ValueError``.
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
    if not timezone:
        trigger = CronTrigger.from_crontab(" ".join(fields))
    else:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            tz = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {timezone!r}") from exc
        trigger = CronTrigger.from_crontab(" ".join(fields), timezone=tz)
    if end_date is not None:
        trigger.end_date = end_date.astimezone(trigger.timezone)
    return trigger


def _parse_iso(value: Any) -> Optional[datetime]:
    """An ISO timestamp column, tz-aware (naive = UTC), or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


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
        self._repo = SchedulerRepository(db_manager)

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
        item_id: Optional[int] = None,
        timezone: Optional[str] = None,
        ends_at: Optional[datetime] = None,
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
            item_id: the ``items`` row this job fires (dv-752a); the handler
                then reads the live item and runs as its assignee.
            timezone: IANA zone the cron fields are read in (None = UTC).
            ends_at: last instant a cron job may fire (None = forever).

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
            trigger = build_cron_trigger(
                cron_expression or "", timezone=timezone, end_date=ends_at
            )
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
                "item_id": item_id,
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
            item_id=item_id,
            timezone=timezone if run_at is None else None,
            ends_at=ends_at if run_at is None else None,
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

    async def remove_jobs_for_item(self, item_id: int) -> int:
        """Remove every active job that fires ``item_id``; returns the count."""
        job_ids = await self._repo.active_job_ids_for_item(item_id)
        for job_id in job_ids:
            await self.remove_job(job_id)
        return len(job_ids)

    def next_run_time(self, job_id: str) -> Optional[datetime]:
        """The live APScheduler next fire time for a job, if registered."""
        job = self._scheduler.get_job(job_id)
        if job is None:
            return None
        return getattr(job, "next_run_time", None)

    async def list_jobs(self) -> List[Dict[str, Any]]:
        """List all scheduled jobs from the database."""
        return await self._repo.list_active_jobs()

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
        item_id: Optional[int] = None,
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
            item_id=item_id,
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
            rows = await self._repo.list_active_jobs()

            for row_dict in rows:
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
                        ends_at = _parse_iso(row_dict.get("ends_at"))
                        if ends_at is not None and ends_at <= datetime.now(UTC):
                            await self._delete_job(row_dict["job_id"])
                            logger.info(
                                "Dropped ended cron job",
                                job_id=row_dict["job_id"],
                                job_name=row_dict["job_name"],
                            )
                            continue
                        trigger = build_cron_trigger(
                            expression,
                            timezone=row_dict.get("timezone") or None,
                            end_date=ends_at,
                        )

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
                            "item_id": row_dict.get("item_id"),
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
        item_id: Optional[int] = None,
        timezone: Optional[str] = None,
        ends_at: Optional[datetime] = None,
    ) -> None:
        """Persist a job definition to the database."""
        chat_ids_str = ",".join(str(cid) for cid in target_chat_ids)
        await self._repo.save_job(
            job_id=job_id,
            job_name=job_name,
            cron_expression=cron_expression,
            prompt=prompt,
            target_chat_ids_csv=chat_ids_str,
            working_directory=working_directory,
            skill_name=skill_name,
            created_by=created_by,
            delivery=delivery,
            item_id=item_id,
            timezone=timezone,
            ends_at_iso=ends_at.astimezone(UTC).isoformat() if ends_at else None,
        )

    async def _delete_job(self, job_id: str) -> None:
        """Soft-delete a job from the database."""
        await self._repo.deactivate_job(job_id)
