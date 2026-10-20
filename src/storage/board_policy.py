"""Who may do what on the task board, and the pump's privilege flag (dv-535b).

One module so the agent tools (``src/claude/agent_tools.py``), the internal
HTTP API (``src/api/server.py``), the taskpump and the merge confirmation
rule (``src/security/destructive.py``) apply the same rules.

Owner decisions, 2026-10-18:

* Only ``devops`` changes a task's status. Every other agent may only
  **create** tasks and **comment** on them.
* A task another agent files for devops (``assignee: devops``) is created
  ``in_progress``, labelled ``agent``, so the pump analyses it and asks the
  owner. Implementation still needs the owner's ``taskpump answer``.
* Any agent may comment on any open task; its comments are marked
  ``from <slug> (agent)``, count <=10 a day, and the pump treats them as data.
* Limits per agent: <=3 open devops tasks, <=5 new tasks per UTC day, exact
  duplicate titles return the open task instead of filing another.
* A pump branch built for an agent-originated task that touches a privileged
  path is flagged on the review card and merging it asks the owner
  (``pump_privileged`` frontmatter). This covers the likely routes only; the
  residual risk is in ``docs/DECISIONS.md`` 2026-10-18.

Stdlib only: the taskpump imports this before it re-execs into the venv.
"""

from __future__ import annotations

import fnmatch
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# The one profile allowed to change task status (and to file tasks without the
# agent rules). Exact slug: a guest workspace of it (``devops#u123``) is not it.
PRIVILEGED_PROFILE = "devops"

# Label every agent-originated devops task carries. The pump keys its agent
# cap, the "from <agent>" header and the privilege check off it.
AGENT_LABEL = "agent"

MAX_OPEN_AGENT_TASKS = 3
MAX_AGENT_TASKS_PER_DAY = 5
MAX_COMMENT_CHARS = 2000
MAX_COMMENTS_PER_DAY = 10  # per agent, across all tasks (UTC day)

# Marker every non-devops agent comment carries; the pump keys its
# "untrusted data" prompt note off it.
AGENT_COMMENT_RE = re.compile(r"^- \d{4}-\d{2}-\d{2} from \S+ \(agent\): ", re.M)

# Paths (repo-relative, fnmatch) whose change by an agent-originated pump
# build needs the owner's explicit confirmation before merge. The likely ways
# an agent's task could widen its own rights -- not all of them.
PRIVILEGED_PATHS: tuple[str, ...] = (
    "config/profiles*.yaml",  # sandbox, extra_dirs, sandbox_read_dirs, tools, models
    "config/mcp*.json",  # which tools/servers an agent gets
    "src/security/*",  # policy, denylist, ask rules, bash box, keystore
    "src/claude/agent_tools.py",  # agent tool rights
    "src/claude/tool_context.py",
    "src/claude/sdk_integration.py",  # allowed tools, hooks, sandbox wiring
    "src/storage/board_policy.py",  # this file
    "src/agent_bridge/*",  # policy gate for Codex
    "src/agent_backends/*",  # sandbox flags per backend
    "src/profiles/*",  # profile parsing (sandbox defaults)
    "scripts/taskpump",  # the pump itself
    "scripts/bgjob",
    "scripts/agentjob",
    "config/taskpump/*",
    "deploy/*",  # systemd units, sudoers
    "*.service",
    "*.timer",
    "*sudoers*",
    "*secret*",
    "*keystore*",
    ".env*",
)

# Where merge confirmation looks for a pump task's frontmatter, besides the
# command's working directory. Tests point this at a tmp board.
TASK_DIRS: List[Path] = [Path(__file__).resolve().parents[2] / ".deviz" / "tasks"]

_PUMP_BRANCH = re.compile(r"\bpump/(dv-[0-9a-z]{3,8})\b")
_GIT_INTEGRATE = re.compile(
    r"\bgit\b[^;&|\n]*\b(merge|cherry-pick|rebase|pull|reset|checkout|restore|"
    r"switch|branch|update-ref|push|am|apply|read-tree)\b"
)


def base_slug(slug: Optional[str]) -> str:
    return (slug or "").split("#", 1)[0].strip()


def is_privileged(slug: Optional[str]) -> bool:
    """True only for the devops profile itself."""
    return (slug or "").strip() == PRIVILEGED_PROFILE


def is_agent_task(labels: Iterable[str]) -> bool:
    return AGENT_LABEL in labels


def wants_devops(assignee: Optional[str]) -> bool:
    return base_slug(assignee) == PRIVILEGED_PROFILE


def privileged_paths(paths: Iterable[str]) -> List[str]:
    """The subset of ``paths`` matching PRIVILEGED_PATHS, order kept."""
    out = []
    for p in paths:
        p = p.strip()
        if p.startswith("./"):
            p = p[2:]
        if not p:
            continue
        name = p.rsplit("/", 1)[-1]
        if any(
            fnmatch.fnmatch(p, pat) or ("/" not in pat and fnmatch.fnmatch(name, pat))
            for pat in PRIVILEGED_PATHS
        ):
            out.append(p)
    return out


# --------------------------------------------------------------- task_add


async def file_agent_task(
    board: Any,
    *,
    slug: str,
    title: str,
    body: Optional[str],
    assignee: Optional[str],
    due_at: Optional[datetime],
) -> Dict[str, Any]:
    """Create a task for a non-devops agent under the agent rules.

    Returns ``{"status": "created"|"duplicate"|"limit", ...}``; only
    ``created`` wrote a file. ``board`` is a ``DevizBoard``.
    """
    origin = base_slug(slug)
    title = " ".join(title.split())
    existing = [
        t for t in await board.list_tasks(status=None, limit=100000) if t is not None
    ]
    for t in existing:
        if t.is_open and t.title == title:
            return {
                "status": "duplicate",
                "task_id": t.id,
                "task_status": t.status,
                "message": "an open task with this title already exists",
            }

    mine = [t for t in existing if base_slug(t.created_by) == origin]
    today = datetime.now(UTC).date()
    created_today = sum(
        1 for t in mine if t.created_at is not None and t.created_at.date() == today
    )
    if created_today >= MAX_AGENT_TASKS_PER_DAY:
        return {
            "status": "limit",
            "message": f"{origin} already filed {created_today} tasks today "
            f"(max {MAX_AGENT_TASKS_PER_DAY}); add to an existing task with "
            "task_comment or try tomorrow",
        }

    for_devops = wants_devops(assignee)
    if for_devops:
        open_devops = [t for t in mine if t.is_open and is_agent_task(t.labels)]
        if len(open_devops) >= MAX_OPEN_AGENT_TASKS:
            return {
                "status": "limit",
                "message": f"{origin} already has {len(open_devops)} open tasks "
                f"for devops (max {MAX_OPEN_AGENT_TASKS}): "
                + ", ".join(t.id for t in open_devops)
                + "; comment on one of them instead",
            }

    labels = [origin] + ([AGENT_LABEL] if for_devops else [])
    task_id = await board.add_task(
        title=title,
        body=body,
        created_by=slug,
        assignee=PRIVILEGED_PROFILE if for_devops else (assignee or None),
        due_at=due_at,
        labels=labels,
        # in_progress with no pump key = the pump analyses it next tick and
        # asks the owner; nothing is implemented without `taskpump answer`.
        status="in_progress" if for_devops else "todo",
    )
    out: Dict[str, Any] = {"status": "created", "task_id": task_id}
    if for_devops:
        out["note"] = (
            "queued for devops: the pump analyses it and asks the owner; "
            "implementation starts only after the owner answers"
        )
    return out


# ------------------------------------------------------------ task_comment


def has_agent_comments(text: str) -> bool:
    return bool(AGENT_COMMENT_RE.search(text))


def comments_today(text: str, slug: str) -> int:
    """Count today's agent-marked comments by ``slug`` in one task file."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    return len(
        re.findall(
            rf"^- {stamp} from {re.escape(base_slug(slug))} \(agent\): ", text, re.M
        )
    )


def comment_line(slug: str, text: str) -> str:
    """devops writes ``@devops:``; any other agent ``from <slug> (agent):``."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    body = " ".join(text.split())
    if is_privileged(slug):
        return f"{stamp} @{slug}: {body}"
    return f"{stamp} from {base_slug(slug)} (agent): {body}"


# ------------------------------------------------------- merge confirmation


def _task_privileged(task_id: str, dirs: Iterable[Path]) -> str:
    """The ``pump_privileged`` frontmatter value of a task, '' if none."""
    for d in dirs:
        for path in (d / f"{task_id}.md", d / "archive" / f"{task_id}.md"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            head = text.split("\n---", 2)[0] if text.startswith("---") else ""
            for line in head.splitlines():
                if line.startswith("pump_privileged:"):
                    return line.split(":", 1)[1].strip()
            return ""
    return ""


def privileged_merge_reason(
    command: str, working_directory: Optional[Path] = None
) -> Optional[str]:
    """Ask-reason when a git command brings a flagged pump branch in."""
    if "pump/" not in command or not _GIT_INTEGRATE.search(command):
        return None
    dirs = list(TASK_DIRS)
    if working_directory is not None:
        dirs.insert(0, Path(working_directory) / ".deviz" / "tasks")
    for task_id in _PUMP_BRANCH.findall(command):
        flagged = _task_privileged(task_id, dirs)
        if flagged:
            return (
                f"pump/{task_id} is an agent-filed change touching privileged "
                f"paths ({flagged[:200]}); confirm the merge"
            )
    return None
