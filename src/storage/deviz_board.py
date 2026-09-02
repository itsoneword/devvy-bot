"""The task board: markdown files under ``.deviz/tasks/``.

Replaces the SQLite ``agent_tasks`` table (dv-1327). One board for agents and
the owner, one id format (``dv-xxxx``). The owner's kanban UI, the taskpump,
the deviz CLI and the agents' ``task_add``/``task_list``/``task_done`` tools
now all read and write the same files.

Deliberately a hand-rolled parser rather than PyYAML: deviz writes frontmatter
unquoted, and real titles contain colons (``dv-1106`` starts "Coach делает
выводы о трендах по памяти, а не из логов — три ложных..."), which
``yaml.safe_load`` rejects. The CLI is the format's owner; we match what it
writes, not what YAML would like it to be.

Unknown frontmatter keys survive a read/write round-trip untouched -- the
taskpump stores ``pump``/``pump_job``/``pump_branch`` there and must not lose
them when an agent marks a task done.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger()

# Statuses the board understands (mirrors .deviz/config.json). Everything that
# is not `done` counts as open for the agent-facing tools.
DONE_STATUS = "done"
DEFAULT_STATUS = "todo"
VALID_STATUSES = ("todo", "in_progress", "review", "waiting_user", "done")

DEFAULT_PRIORITY = "medium"
ID_PREFIX = "dv"

# Frontmatter keys we own, in the order the deviz CLI writes them. Anything
# else found in a file is preserved and appended after these.
_KNOWN_ORDER = (
    "id",
    "title",
    "status",
    "priority",
    "assignee",
    "labels",
    "deps",
    "parent",
    "created",
    "updated",
)

_FM_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
_SECTION_RE = re.compile(r"^## +(.+?) *$", re.MULTILINE)


def _now_iso() -> str:
    """UTC, second precision, ``Z`` suffix -- the format deviz writes."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class DevizTask:
    """One task file. ``id`` is a string (``dv-3668``), not an int."""

    id: str
    title: str
    body: Optional[str] = None
    created_by: str = "user"
    assignee: Optional[str] = None
    due_at: Optional[datetime] = None
    status: str = DEFAULT_STATUS
    priority: str = DEFAULT_PRIORITY
    labels: List[str] = field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    path: Optional[Path] = None

    @property
    def is_open(self) -> bool:
        return self.status != DONE_STATUS

    def to_dict(self) -> Dict[str, Any]:
        """Shape kept close to the old ``AgentTask.to_dict`` so the HTTP API
        and the `/tasks` command needed no reshaping -- only ``id`` changed
        type."""
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "created_by": self.created_by,
            "assignee": self.assignee,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "status": self.status,
            "priority": self.priority,
            "labels": list(self.labels),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def _parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """``(frontmatter dict, body)``. Values stay raw strings."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fm: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        fm[key.strip()] = value.strip()
    return fm, m.group(2)


def _parse_list(raw: str) -> List[str]:
    """``[coach, persona]`` -> ``["coach", "persona"]``."""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [part.strip() for part in raw.split(",") if part.strip()]


def _section(body: str, name: str) -> Optional[str]:
    """Text under ``## <name>`` up to the next ``##`` heading."""
    matches = list(_SECTION_RE.finditer(body))
    for i, m in enumerate(matches):
        if m.group(1).strip().lower() != name.lower():
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        return body[m.end() : end].strip() or None
    return None


class DevizBoard:
    """Async file-backed board with the interface ``TaskRepository`` had.

    The methods are ``async`` and do their I/O in a worker thread: callers sit
    on the bot's event loop, and a board of ~60 files is read in full on every
    ``list_tasks``.
    """

    def __init__(self, tasks_dir: Path | str = Path(".deviz/tasks")) -> None:
        self._dir = Path(tasks_dir).expanduser().resolve()
        self._lock = asyncio.Lock()

    @property
    def tasks_dir(self) -> Path:
        return self._dir

    # ------------------------------------------------------------------ read

    def _iter_files(self, include_archive: bool = True) -> List[Path]:
        files = sorted(self._dir.glob("*.md"))
        archive = self._dir / "archive"
        if include_archive and archive.is_dir():
            files += sorted(archive.glob("*.md"))
        return files

    def _read(self, path: Path) -> Optional[DevizTask]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Unreadable task file", path=str(path), error=str(e))
            return None
        fm, body = _parse_frontmatter(text)
        task_id = fm.get("id") or path.stem
        if not fm.get("title"):
            logger.warning("Task file without a title", path=str(path))
            return None
        return DevizTask(
            id=task_id,
            title=fm["title"],
            body=_section(body, "Description"),
            created_by=fm.get("created_by") or "user",
            assignee=fm.get("assignee") or None,
            due_at=_parse_dt(fm.get("due")),
            status=fm.get("status") or DEFAULT_STATUS,
            priority=fm.get("priority") or DEFAULT_PRIORITY,
            labels=_parse_list(fm.get("labels", "")),
            created_at=_parse_dt(fm.get("created")),
            updated_at=_parse_dt(fm.get("updated")),
            path=path,
        )

    def _find(self, task_id: str) -> Optional[Path]:
        task_id = task_id.strip()
        for candidate in (
            self._dir / f"{task_id}.md",
            self._dir / "archive" / f"{task_id}.md",
        ):
            if candidate.is_file():
                return candidate
        return None

    async def get_task(self, task_id: str) -> Optional[DevizTask]:
        def _work() -> Optional[DevizTask]:
            path = self._find(task_id)
            return self._read(path) if path else None

        return await asyncio.to_thread(_work)

    async def list_tasks(
        self, status: Optional[str] = "open", limit: int = 50
    ) -> List[DevizTask]:
        """``status``: ``open`` (anything not done), ``done``, a concrete deviz
        status, or None for everything. Ordered like the old board: tasks with
        a due date first and soonest-first, then the rest newest-first."""

        def _work() -> List[DevizTask]:
            # Archived files are all done; skip the extra I/O unless asked for.
            want_archive = status in (None, "all", DONE_STATUS)
            tasks = [
                t for t in (self._read(p) for p in self._iter_files(want_archive)) if t
            ]
            if status == "open":
                tasks = [t for t in tasks if t.is_open]
            elif status in (DONE_STATUS,):
                tasks = [t for t in tasks if not t.is_open]
            elif status not in (None, "all"):
                tasks = [t for t in tasks if t.status == status]
            tasks.sort(
                key=lambda t: (
                    t.due_at is None,
                    t.due_at or datetime.max.replace(tzinfo=UTC),
                    -(t.created_at.timestamp() if t.created_at else 0),
                )
            )
            return tasks[:limit]

        return await asyncio.to_thread(_work)

    # ----------------------------------------------------------------- write

    def _new_id(self) -> str:
        for _ in range(100):
            candidate = f"{ID_PREFIX}-{secrets.token_hex(2)}"
            if not self._find(candidate):
                return candidate
        raise RuntimeError("could not allocate a free task id")

    def _render(self, fm: Dict[str, str], body: str) -> str:
        ordered = [k for k in _KNOWN_ORDER if k in fm]
        ordered += [k for k in fm if k not in _KNOWN_ORDER]
        lines = "\n".join(f"{k}: {fm[k]}" for k in ordered)
        return f"---\n{lines}\n---\n{body}"

    async def add_task(
        self,
        title: str,
        body: Optional[str] = None,
        created_by: str = "user",
        assignee: Optional[str] = None,
        due_at: Optional[datetime] = None,
        priority: str = DEFAULT_PRIORITY,
        labels: Optional[List[str]] = None,
        status: str = DEFAULT_STATUS,
    ) -> str:
        """Create a task file. Returns the new ``dv-xxxx`` id."""

        def _work() -> str:
            self._dir.mkdir(parents=True, exist_ok=True)
            task_id = self._new_id()
            now = _now_iso()
            # created_by is ours, not deviz's -- attribution is the whole
            # reason the shared board existed, and the CLI has no field for it.
            tags = list(labels or [])
            fm = {
                "id": task_id,
                "title": " ".join(title.split()),
                "status": status if status in VALID_STATUSES else DEFAULT_STATUS,
                "priority": priority,
                "assignee": assignee or "",
                "labels": f"[{', '.join(tags)}]",
                "deps": "[]",
                "parent": "",
                "created": now,
                "updated": now,
                "created_by": created_by,
            }
            if due_at is not None:
                fm["due"] = due_at.isoformat()
            sections = f"\n## Description\n\n{(body or title).strip()}\n\n"
            sections += "## Acceptance Criteria\n\n## Notes\n\n## Comments\n"
            (self._dir / f"{task_id}.md").write_text(
                self._render(fm, sections), encoding="utf-8"
            )
            return task_id

        async with self._lock:
            task_id = await asyncio.to_thread(_work)
        logger.info("Task created", task_id=task_id, created_by=created_by)
        return task_id

    async def set_status(self, task_id: str, status: str) -> bool:
        """Set a task's status in place. ``open`` maps to ``todo``.

        Rewrites only the ``status``/``updated`` keys -- pump keys and any hand
        -written frontmatter survive.
        """
        if status == "open":
            status = DEFAULT_STATUS
        if status not in VALID_STATUSES:
            raise ValueError(f"unknown status: {status}")

        def _work() -> bool:
            path = self._find(task_id)
            if path is None:
                return False
            fm, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
            if not fm:
                return False
            fm["status"] = status
            fm["updated"] = _now_iso()
            path.write_text(self._render(fm, body), encoding="utf-8")
            return True

        async with self._lock:
            ok = await asyncio.to_thread(_work)
        logger.info("Task status set", task_id=task_id, status=status, found=ok)
        return ok

    async def append_comment(self, task_id: str, comment: str) -> bool:
        """Append a bullet under ``## Comments``. Used by the consolidation
        service to add evidence to an existing open task instead of filing a
        duplicate."""

        def _work() -> bool:
            path = self._find(task_id)
            if path is None:
                return False
            fm, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
            line = f"- {comment.strip()}\n"
            if "## Comments" in body:
                body = body.rstrip("\n") + "\n" + line
            else:
                body = body.rstrip("\n") + "\n\n## Comments\n\n" + line
            fm["updated"] = _now_iso()
            path.write_text(self._render(fm, body), encoding="utf-8")
            return True

        async with self._lock:
            return await asyncio.to_thread(_work)

    async def find_open_by_title(self, title: str) -> Optional[DevizTask]:
        """First open task with this exact title. The consolidation service's
        dedupe key -- its titles are generated (``Consolidation needed:
        <scope>``) and therefore stable per scope."""
        for task in await self.list_tasks(status="open", limit=500):
            if task.title == title:
                return task
        return None
