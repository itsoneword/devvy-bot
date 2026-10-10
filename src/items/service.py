"""Family items: todos and reminders across family members (dv-752a phase A).

One ``items`` row is the source of truth; when it has a time it also gets one
scheduler job (``scheduled_jobs.item_id``) that fires into the ASSIGNEE's chat
and workspace, in the agent the creator named (default ``general``).

Rules (owner decisions 2026-09-28):

* trust model = family: the creator OR the assignee may read, edit, complete
  and cancel an item; nobody else. There is no mute / opt-out — reminders
  always arrive; a member can only cancel all reminders set for them.
* loop guard: a scheduler-driven turn cannot create or retarget items for
  ANOTHER member (:mod:`.loop_guard`), and one creator may set at most
  ``cross_member_hourly_cap`` items for others per hour.
* a reminder for another member is delivered as a message; ``deliver=agent``
  (an agent turn in their workspace) is self-only in phase A (draft D1).
* times: a bare ``at`` and every ``cron`` are read in the ASSIGNEE's zone and
  the item stores ``tz``, so a recurring reminder does not drift at DST.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

import structlog

from .loop_guard import is_automated
from .repository import (
    DELIVERIES,
    STATUSES,
    Item,
    NOTE_KINDS,
    NOTE_SOURCES,
    ItemRepository,
    Member,
    now_iso,
    parse_iso,
)

if TYPE_CHECKING:
    from ..profiles.registry import ProfileRegistry

logger = structlog.get_logger()

MAX_TEXT_CHARS = 1000
MAX_NOTE_CHARS = 4000
NOTE_PREVIEW_CHARS = 300
CLEAR_WORDS = frozenset({"none", "clear", "null", "-"})
DEFAULT_TARGET_AGENT = "general"
SELF_WORDS = frozenset(
    {"", "me", "self", "myself", "я", "мне", "меня", "себе", "себя", "сам", "сама"}
)
SCOPES = ("all", "mine", "by_me", "for_me")


class ItemError(Exception):
    """A refusal the agent (or the /reminders UI) can relay verbatim."""


@dataclass
class ItemEvent:
    """One line of turn-start context."""

    at: str
    text: str


def _local_zone(name: str) -> Any:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _fmt_local(value: Optional[datetime], tz_name: str) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(_local_zone(tz_name)).strftime("%Y-%m-%d %H:%M")


class ItemService:
    """Business rules over :class:`ItemRepository` + the job scheduler."""

    def __init__(
        self,
        repo: ItemRepository,
        scheduler: Any = None,
        profile_registry: Optional["ProfileRegistry"] = None,
        default_tz: str = "UTC",
        role_lookup: Optional[Callable[[int], Optional[str]]] = None,
        cross_member_hourly_cap: int = 20,
    ) -> None:
        self.repo = repo
        self.scheduler = scheduler
        self.registry = profile_registry
        self.default_tz = default_tz or "UTC"
        self.role_lookup = role_lookup
        self.cross_member_hourly_cap = cross_member_hourly_cap

    # ------------------------------------------------------------------
    # members
    # ------------------------------------------------------------------

    def tz_for(self, user_id: int) -> str:
        """The member's zone. Install-wide until per-user tz lands (phase C);
        this is the one place to swap in a per-user value."""
        return self.default_tz

    async def member(self, user_id: int) -> Member:
        found = await self.repo.get_member(user_id)
        if found is not None:
            return found
        role = self.role_lookup(user_id) if self.role_lookup else None
        return Member(user_id, role or "guest", None, None, None, False)

    async def roster(self, user_id: int) -> List[Member]:
        """Everyone in this user's family (self included), or just self."""
        me = await self.member(user_id)
        if me.family_id is None:
            return [me]
        members = await self.repo.family_members(me.family_id)
        if not any(m.telegram_id == user_id for m in members):
            members.insert(0, me)
        return members

    async def names(self, user_id: int) -> Dict[int, str]:
        return {m.telegram_id: m.label for m in await self.roster(user_id)}

    async def resolve_member(self, user_id: int, who: str) -> Member:
        """``who`` -> a family member. Never falls back to self on a miss."""
        members = await self.roster(user_id)
        key = (who or "").strip()
        if key.casefold() in SELF_WORDS or key == str(user_id):
            return next(m for m in members if m.telegram_id == user_id)
        if key.lstrip("-").isdigit():
            hit = [m for m in members if m.telegram_id == int(key)]
        else:
            folded = key.casefold()
            hit = [
                m
                for m in members
                if folded
                in {
                    (m.display_name or "").casefold(),
                    (m.relation or "").casefold(),
                }
            ]
        if len(hit) == 1:
            return hit[0]
        listing = "; ".join(
            f"{m.label} (id {m.telegram_id}"
            + (f", {m.relation}" if m.relation else "")
            + ")"
            for m in members
        )
        if not hit:
            raise ItemError(
                f"'{who}' is not a member of this user's family. Members: "
                f"{listing}. Pass the member's id."
            )
        raise ItemError(f"'{who}' is ambiguous. Members: {listing}. Pass the id.")

    async def set_member(
        self,
        user_id: int,
        who: str,
        display_name: Optional[str],
        relation: Optional[str],
    ) -> Member:
        target = await self.resolve_member(user_id, who)
        name = (display_name or "").strip() or None
        rel = (relation or "").strip() or None
        if name is None and rel is None:
            raise ItemError("give a name and/or a relation")
        if (name and len(name) > 60) or (rel and len(rel) > 60):
            raise ItemError("name and relation must be at most 60 characters")
        if not await self.repo.set_member_fields(target.telegram_id, name, rel):
            raise ItemError("that member has no bot account row to update")
        return await self.member(target.telegram_id)

    async def family_line(self, user_id: int) -> str:
        """Compact roster for the turn prompt; '' when there is no family."""
        members = await self.roster(user_id)
        if len(members) < 2:
            return ""
        parts = []
        for m in members:
            extra = [f"id {m.telegram_id}"]
            if m.relation:
                extra.append(m.relation)
            if m.telegram_id == user_id:
                extra.append("this user")
            parts.append(f"{m.label} ({', '.join(extra)})")
        return "Family members: " + "; ".join(parts) + "."

    # ------------------------------------------------------------------
    # schedule parsing
    # ------------------------------------------------------------------

    def _parse_when(
        self, at: Optional[str], cron: Optional[str], tz_name: str
    ) -> Tuple[Optional[datetime], Optional[str]]:
        at = (at or "").strip() or None
        cron = (cron or "").strip() or None
        if at and cron:
            raise ItemError("give 'at' OR 'cron', not both")
        if at:
            try:
                due = datetime.fromisoformat(at)
            except ValueError as exc:
                raise ItemError(f"could not parse 'at' datetime: {at!r}") from exc
            if due.tzinfo is None:
                due = due.replace(tzinfo=_local_zone(tz_name))
            if due <= datetime.now(UTC):
                raise ItemError("'at' is in the past")
            return due.astimezone(UTC), None
        if cron:
            from ..scheduler.scheduler import build_cron_trigger

            if len(cron.split()) != 5:
                raise ItemError("cron must have 5 fields (min hour dom month dow)")
            try:
                build_cron_trigger(cron, timezone=tz_name)
            except ValueError as exc:
                raise ItemError(f"invalid cron {cron!r}: {exc}") from exc
            return None, cron
        return None, None

    def _parse_deadline(self, value: str, tz_name: str) -> datetime:
        """Local date (-> end of that day, 23:59) or local datetime -> UTC."""
        raw = value.strip()
        try:
            if len(raw) == 10:
                day = datetime.strptime(raw, "%Y-%m-%d")
                due = day.replace(hour=23, minute=59)
            else:
                due = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ItemError(
                f"could not parse 'deadline': {raw!r} (use YYYY-MM-DD or a local "
                "ISO datetime)"
            ) from exc
        if due.tzinfo is None:
            due = due.replace(tzinfo=_local_zone(tz_name))
        return due.astimezone(UTC)

    def next_fire(self, item: Item) -> Optional[datetime]:
        if item.status != "open":
            return None
        if item.due_at:
            return parse_iso(item.due_at)
        if item.recurrence:
            from ..scheduler.scheduler import build_cron_trigger

            try:
                trigger = build_cron_trigger(
                    item.recurrence, timezone=item.tz, end_date=parse_iso(item.ends_at)
                )
            except ValueError:
                return None
            nxt: Optional[datetime] = trigger.get_next_fire_time(
                None, datetime.now(UTC)
            )
            return nxt
        return None

    # ------------------------------------------------------------------
    # target agent
    # ------------------------------------------------------------------

    def _resolve_agent(
        self,
        slug: Optional[str],
        assignee: Member,
        cross: bool,
        own_profile: str,
        own_working_dir: Optional[Path],
    ) -> Tuple[Optional[str], Optional[Path]]:
        base_slug = (own_profile or "").split("#", 1)[0] or None
        wanted = (slug or "").strip().lower() or None
        if self.registry is None:
            return wanted or base_slug, own_working_dir
        if wanted is None:
            candidates = (
                [DEFAULT_TARGET_AGENT, base_slug]
                if cross
                else [base_slug, DEFAULT_TARGET_AGENT]
            )
            wanted = next(
                (
                    c
                    for c in candidates
                    if c and self.registry.get_enabled_by_slug(c) is not None
                ),
                None,
            )
        role = assignee.role or "guest"
        profile = self.registry.get_enabled_by_slug(wanted) if wanted else None
        if profile is None or not profile.allows_role(role):
            usable = [
                p.slug for p in self.registry.enabled_profiles if p.allows_role(role)
            ]
            raise ItemError(
                f"agent '{wanted}' is not available to {assignee.label}; "
                f"choose one of: {', '.join(usable)}"
            )
        return profile.slug, profile.working_dir

    # ------------------------------------------------------------------
    # guards
    # ------------------------------------------------------------------

    async def _guard_cross_member(self, creator_id: int, count_new: bool) -> None:
        if is_automated(creator_id):
            logger.warning("item loop guard refused", creator_id=creator_id)
            raise ItemError(
                "this turn was started by a reminder/scheduled job, so it cannot "
                "create or change reminders for other family members (loop "
                "guard). Tell the user instead; they can ask for it themselves."
            )
        if count_new:
            since = (datetime.now(UTC) - timedelta(hours=1)).isoformat(
                timespec="microseconds"
            )
            recent = await self.repo.count_cross_created_since(creator_id, since)
            if recent >= self.cross_member_hourly_cap:
                raise ItemError(
                    f"limit reached: at most {self.cross_member_hourly_cap} "
                    "reminders for other family members per hour"
                )

    async def _authorized(self, user_id: int, item_id: int) -> Item:
        item = await self.repo.get(item_id)
        if item is None or user_id not in (item.creator_id, item.assignee_id):
            raise ItemError(f"no item #{item_id} that you created or that is for you")
        return item

    # ------------------------------------------------------------------
    # scheduler wiring
    # ------------------------------------------------------------------

    async def _register_job(self, item: Item, working_dir: Optional[Path]) -> None:
        if not item.is_scheduled or item.status != "open":
            return
        if self.scheduler is None:
            raise ItemError("the scheduler is disabled; the item has no reminder")
        await self.scheduler.add_job(
            job_name=f"item#{item.id}: {item.text[:40]}",
            cron_expression=item.recurrence,
            run_at=parse_iso(item.due_at) if item.due_at else None,
            prompt=item.text,
            target_chat_ids=[item.assignee_id],
            working_directory=working_dir,
            created_by=item.creator_id,
            delivery="agent" if item.deliver == "agent" else "message",
            item_id=item.id,
            timezone=item.tz if item.recurrence else None,
            ends_at=parse_iso(item.ends_at),
        )

    async def _unregister(self, item_id: int) -> None:
        if self.scheduler is not None:
            await self.scheduler.remove_jobs_for_item(item_id)

    def _working_dir_for(self, slug: Optional[str]) -> Optional[Path]:
        if self.registry is None or not slug:
            return None
        profile = self.registry.get_by_slug(slug)
        return profile.working_dir if profile else None

    # ------------------------------------------------------------------
    # operations
    # ------------------------------------------------------------------

    async def add(
        self,
        creator_id: int,
        text: str,
        who: str = "",
        at: Optional[str] = None,
        cron: Optional[str] = None,
        agent: Optional[str] = None,
        deliver: Optional[str] = None,
        source_profile: str = "",
        source_working_dir: Optional[Path] = None,
        ends_at: Optional[datetime] = None,
        deadline: Optional[str] = None,
    ) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise ItemError("text is required")
        if len(text) > MAX_TEXT_CHARS:
            raise ItemError(f"text must be at most {MAX_TEXT_CHARS} characters")
        creator = await self.member(creator_id)
        assignee = await self.resolve_member(creator_id, who)
        cross = assignee.telegram_id != creator_id
        if cross:
            await self._guard_cross_member(creator_id, count_new=True)
        tz_name = self.tz_for(assignee.telegram_id)
        due, recurrence = self._parse_when(at, cron, tz_name)
        deadline_utc = (
            self._parse_deadline(deadline, tz_name)
            if (deadline or "").strip()
            else None
        )
        scheduled = bool(due or recurrence)
        mode = (deliver or "").strip().lower() or ("message" if scheduled else "none")
        if mode not in DELIVERIES:
            raise ItemError("deliver must be message | agent | none")
        if not scheduled:
            mode = "none"
        elif mode == "none":
            raise ItemError("a timed item needs deliver=message or agent")
        if cross and mode == "agent":
            raise ItemError(
                "reminders for another family member are delivered as a message "
                "(deliver=message); deliver=agent is only for your own reminders"
            )
        slug, working_dir = self._resolve_agent(
            agent, assignee, cross, source_profile, source_working_dir
        )
        item_id = await self.repo.insert(
            family_id=creator.family_id,
            creator_id=creator_id,
            assignee_id=assignee.telegram_id,
            text=text,
            status="open",
            due_at=due.isoformat(timespec="seconds") if due else None,
            recurrence=recurrence,
            deadline=(
                deadline_utc.isoformat(timespec="seconds") if deadline_utc else None
            ),
            tz=tz_name,
            ends_at=(
                ends_at.astimezone(UTC).isoformat(timespec="seconds")
                if ends_at
                else None
            ),
            deliver=mode,
            profile_slug=slug,
            source_profile=(source_profile or "").split("#", 1)[0] or None,
        )
        item = await self.repo.get(item_id)
        assert item is not None
        try:
            await self._register_job(item, working_dir)
        except Exception as exc:
            await self.repo.update(item_id, status="cancelled", done_at=now_iso())
            if isinstance(exc, ItemError):
                raise
            logger.exception("item job registration failed", item_id=item_id)
            raise ItemError(f"could not schedule the reminder: {exc}") from exc
        logger.info(
            "item added",
            item_id=item_id,
            creator_id=creator_id,
            assignee_id=assignee.telegram_id,
            agent=slug,
            deliver=mode,
            recurring=bool(recurrence),
        )
        return {**await self.describe(item, creator_id), "status": "created"}

    async def update(
        self,
        user_id: int,
        item_id: int,
        text: Optional[str] = None,
        at: Optional[str] = None,
        cron: Optional[str] = None,
        agent: Optional[str] = None,
        deliver: Optional[str] = None,
        deadline: Optional[str] = None,
    ) -> Dict[str, Any]:
        item = await self._authorized(user_id, item_id)
        if item.status not in ("open", "fired"):
            raise ItemError(f"item #{item_id} is {item.status}; add a new one")
        if item.cross_member:
            await self._guard_cross_member(user_id, count_new=False)
        fields: Dict[str, Any] = {}
        if text is not None and text.strip():
            if len(text.strip()) > MAX_TEXT_CHARS:
                raise ItemError(f"text must be at most {MAX_TEXT_CHARS} characters")
            fields["text"] = text.strip()
        reschedule = False
        if (deadline or "").strip():
            if deadline.strip().lower() in CLEAR_WORDS:
                fields["deadline"] = None
            else:
                fields["deadline"] = self._parse_deadline(deadline, item.tz).isoformat(
                    timespec="seconds"
                )
        if (at or "").strip() or (cron or "").strip():
            due, recurrence = self._parse_when(at, cron, item.tz)
            fields["due_at"] = due.isoformat(timespec="seconds") if due else None
            fields["recurrence"] = recurrence
            fields["status"] = "open"
            if item.deliver == "none" and not (deliver or "").strip():
                fields["deliver"] = "message"
            reschedule = True
        mode = (deliver or "").strip().lower()
        if mode:
            if mode not in ("message", "agent"):
                raise ItemError("deliver must be message | agent")
            if item.cross_member and mode == "agent":
                raise ItemError("deliver=agent is only for your own reminders")
            fields["deliver"] = mode
            reschedule = True
        slug = item.profile_slug
        if (agent or "").strip():
            assignee = await self.member(item.assignee_id)
            slug, _ = self._resolve_agent(
                agent, assignee, item.cross_member, item.profile_slug or "", None
            )
            fields["profile_slug"] = slug
            reschedule = True
        if not fields:
            raise ItemError(
                "nothing to change: give text, deadline, at/cron, agent or deliver"
            )
        await self.repo.update(item_id, **fields)
        updated = await self.repo.get(item_id)
        assert updated is not None
        # A text-only edit needs no re-register: the job's prompt is only a
        # snapshot, the fire path reads the live item.
        if reschedule:
            await self._unregister(item_id)
            await self._register_job(updated, self._working_dir_for(slug))
        logger.info("item updated", item_id=item_id, by=user_id, fields=list(fields))
        return {**await self.describe(updated, user_id), "status": "updated"}

    async def close(self, user_id: int, item_id: int, status: str) -> Dict[str, Any]:
        """Mark done or cancelled; removes the reminder job either way."""
        assert status in ("done", "cancelled")
        item = await self._authorized(user_id, item_id)
        if item.status in ("done", "cancelled"):
            return {"status": f"already_{item.status}", "id": item_id}
        await self._unregister(item_id)
        await self.repo.update(
            item_id, status=status, done_at=now_iso(), done_by=user_id
        )
        await self.repo.add_note(
            item_id, user_id, "status_change", status, source="manual"
        )
        logger.info("item closed", item_id=item_id, by=user_id, status=status)
        return {"status": status, "id": item_id, "text": item.text}

    async def add_note(
        self,
        user_id: int,
        item_id: int,
        kind: str = "comment",
        body: str = "",
        path: Optional[str] = None,
        source: str = "manual",
    ) -> Dict[str, Any]:
        await self._authorized(user_id, item_id)
        kind = (kind or "comment").strip().lower()
        if kind not in NOTE_KINDS:
            raise ItemError(f"kind must be one of {', '.join(NOTE_KINDS)}")
        if source not in NOTE_SOURCES:
            raise ItemError(f"source must be one of {', '.join(NOTE_SOURCES)}")
        body = (body or "").strip()
        if not body:
            raise ItemError("text is required")
        if len(body) > MAX_NOTE_CHARS:
            raise ItemError(
                f"note text must be at most {MAX_NOTE_CHARS} characters; save the "
                "long content to a file and pass its location as 'path', with a "
                "short summary in 'text'"
            )
        path = (path or "").strip() or None
        note_id = await self.repo.add_note(
            item_id, user_id, kind, body, path=path, source=source
        )
        logger.info("item note added", item_id=item_id, by=user_id, kind=kind)
        return {"status": "noted", "id": item_id, "note_id": note_id, "kind": kind}

    async def list_notes(self, user_id: int, item_id: int) -> List[Dict[str, Any]]:
        item = await self._authorized(user_id, item_id)
        return await self._notes_view(item, user_id, await self.names(user_id))

    async def _notes_view(
        self, item: Item, viewer_id: int, names: Dict[int, str]
    ) -> List[Dict[str, Any]]:
        out = []
        for n in await self.repo.list_notes(item.id):
            if n.author_id is None:
                author = "auto"
            elif n.author_id == viewer_id:
                author = "you"
            else:
                author = names.get(n.author_id) or f"id {n.author_id}"
            body = n.body
            if len(body) > NOTE_PREVIEW_CHARS:
                body = body[:NOTE_PREVIEW_CHARS] + "…"
            row: Dict[str, Any] = {
                "kind": n.kind,
                "author": author,
                "date": _fmt_local(parse_iso(n.created_at), item.tz),
                "body": body,
            }
            if n.path:
                row["path"] = n.path
            if n.source == "auto":
                row["source"] = "auto"
            out.append(row)
        return out

    async def cancel_all_mine(self, user_id: int) -> Dict[str, Any]:
        """Cancel every open timed item set FOR this user (by anyone).

        The recipient's one control (owner decision: no mute, no opt-out of
        receiving; only "cancel all my existing reminders")."""
        items = await self.repo.list_for(user_id, "all", ("open",), limit=500)
        cancelled = []
        for item in items:
            if item.assignee_id == user_id and item.is_scheduled:
                await self.close(user_id, item.id, "cancelled")
                cancelled.append(item.id)
        return {"status": "cancelled", "count": len(cancelled), "ids": cancelled}

    async def list(
        self, user_id: int, scope: str = "all", status: str = "open"
    ) -> List[Dict[str, Any]]:
        """Rows without notes; each carries a ``notes`` count."""
        if scope not in SCOPES:
            raise ItemError(f"scope must be one of {', '.join(SCOPES)}")
        if status == "all":
            statuses: Tuple[str, ...] = STATUSES
        elif status in STATUSES:
            statuses = (status,)
        else:
            raise ItemError("status must be open | fired | done | cancelled | all")
        items = await self.repo.list_for(user_id, scope, statuses)
        names = await self.names(user_id)
        return [await self.describe(i, user_id, names) for i in items]

    async def describe(
        self,
        item: Item,
        viewer_id: int,
        names: Optional[Dict[int, str]] = None,
        with_notes: bool = False,
    ) -> Dict[str, Any]:
        names = names if names is not None else await self.names(viewer_id)

        def who(uid: int) -> str:
            if uid == viewer_id:
                return "you"
            return names.get(uid) or f"id {uid}"

        out: Dict[str, Any] = {
            "id": item.id,
            "text": item.text,
            "status": item.status,
            "for": who(item.assignee_id),
            "from": who(item.creator_id),
            "agent": item.profile_slug,
            "deliver": item.deliver,
        }
        if item.deadline:
            out["deadline"] = (
                f"{_fmt_local(parse_iso(item.deadline), item.tz)} {item.tz}"
            )
        if item.recurrence:
            out["recurrence"] = f"{item.recurrence} ({item.tz})"
        nxt = self.next_fire(item)
        if nxt is not None:
            out["next_local"] = f"{_fmt_local(nxt, item.tz)} {item.tz}"
            out["next_utc"] = nxt.astimezone(UTC).isoformat(timespec="minutes")
        if item.last_fired_at:
            out["last_fired_local"] = _fmt_local(parse_iso(item.last_fired_at), item.tz)
        out["notes"] = await self.repo.count_notes(item.id)
        if with_notes and out["notes"]:
            out["notes_list"] = await self._notes_view(item, viewer_id, names)
        return out

    async def get(self, user_id: int, item_id: int) -> Dict[str, Any]:
        """One item with its notes (authorization as for any mutation)."""
        item = await self._authorized(user_id, item_id)
        return await self.describe(item, user_id, with_notes=True)

    # ------------------------------------------------------------------
    # fire path (called by the event handler)
    # ------------------------------------------------------------------

    async def mark_fired(self, item: Item) -> None:
        fields: Dict[str, Any] = {"last_fired_at": now_iso()}
        if not item.recurrence:
            fields["status"] = "fired"
        await self.repo.update(item.id, **fields)

    # ------------------------------------------------------------------
    # turn-start context
    # ------------------------------------------------------------------

    async def turn_context(
        self, user_id: int, profile_slug: str, lookback_hours: int = 24
    ) -> str:
        """``<family_reminders>`` block for this user's turn in this agent.

        Roster (when the user has a family) plus item activity since the last
        turn of this user in this agent: reminders delivered to them here,
        reminders others set for them here, and — from any agent — what
        happened to reminders they set for others. Advances the cursor, so
        each event is injected once. '' when there is nothing to say.
        """
        slug = (profile_slug or "default").split("#", 1)[0]
        stamp = now_iso()
        seen = await self.repo.get_seen(user_id, slug)
        if seen is None:
            seen = (datetime.now(UTC) - timedelta(hours=lookback_hours)).isoformat(
                timespec="microseconds"
            )
        items = await self.repo.activity_since(user_id, seen)
        names = await self.names(user_id)
        await self.repo.set_seen(user_id, slug, stamp)
        events: List[ItemEvent] = []

        def name(uid: int) -> str:
            return names.get(uid) or f"id {uid}"

        tz = self.tz_for(user_id)

        def hm(value: Optional[str]) -> str:
            return _fmt_local(parse_iso(value), tz) or "?"

        for it in items:
            mine_here = it.assignee_id == user_id and it.profile_slug == slug
            set_by_me = it.creator_id == user_id and it.cross_member
            quoted = f"#{it.id} «{it.text[:200]}»"
            if it.created_at > seen:
                if set_by_me and it.source_profile != slug:
                    events.append(
                        ItemEvent(
                            it.created_at,
                            f"{hm(it.created_at)}: this user (via {it.source_profile}"
                            f" agent) set a reminder for {name(it.assignee_id)} in "
                            f"{it.profile_slug}: {quoted}",
                        )
                    )
                elif mine_here and it.cross_member:
                    events.append(
                        ItemEvent(
                            it.created_at,
                            f"{hm(it.created_at)}: {name(it.creator_id)} set a "
                            f"reminder for this user here: {quoted}",
                        )
                    )
            if it.last_fired_at and it.last_fired_at > seen:
                if mine_here:
                    sender = f" from {name(it.creator_id)}" if it.cross_member else ""
                    events.append(
                        ItemEvent(
                            it.last_fired_at,
                            f"{hm(it.last_fired_at)}: reminder{sender} delivered to "
                            f"this user here: {quoted}",
                        )
                    )
                elif set_by_me:
                    events.append(
                        ItemEvent(
                            it.last_fired_at,
                            f"{hm(it.last_fired_at)}: this user's reminder for "
                            f"{name(it.assignee_id)} was delivered ({it.profile_slug}"
                            f"): {quoted}",
                        )
                    )
            if (
                it.done_at
                and it.done_at > seen
                and it.done_by not in (None, user_id)
                and it.status in ("done", "cancelled")
            ):
                events.append(
                    ItemEvent(
                        it.done_at,
                        f"{hm(it.done_at)}: {name(int(it.done_by or 0))} marked "
                        f"{quoted} {it.status}",
                    )
                )
        roster = await self.family_line(user_id)
        if not roster and not events:
            return ""
        lines = ["<family_reminders>"]
        if roster:
            lines.append(roster)
        if events:
            lines.append("Reminder activity since this user's last message here:")
            for ev in sorted(events, key=lambda e: e.at):
                lines.append(f"- {ev.text}")
        lines.append("</family_reminders>")
        return "\n".join(lines)
