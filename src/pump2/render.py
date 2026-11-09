"""Rendering reads state only: card frontmatter and the worktree's .pump/ logs."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from .state import Board, Card, _when, list_cards, worktree_path

LOGS = ("merge.log",)  # stage logs: job-<stage>.log, then merge.log


def last_line(board: Board, card: Card) -> str:
    d = worktree_path(board, card.id) / ".pump"
    st = card.pump2.get("stage", "")
    for name in (f"job-{st}.log", *LOGS):
        p = d / name
        if p.exists():
            lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
            if lines:
                return lines[-1][:120]
    return ""


def minutes(card: Card, now: datetime) -> str:
    if not card.pump2.get("job"):
        return "-"
    since = _when(card.pump2.get("since"))
    return f"{int((now - since).total_seconds() // 60)}m" if since else "?"


def status_lines(
    boards: dict[str, Board], only: str | None = None, now: datetime | None = None
) -> list[str]:
    now = now or datetime.now(UTC)
    rows = []
    for board in boards.values():
        if only and board.name != only:
            continue
        cards, bad = list_cards(board)
        rows += [f"{board.name}:{p.name}  ERROR  {err}" for p, err in bad]
        for c in cards:
            if not c.pump2.get("stage"):
                continue
            held = c.fm.get("status")
            mark = f" [{held}]" if held and held != "in_progress" else ""
            rows.append(
                f"{board.name}:{c.id}  {c.pump2['stage']}{mark}  "
                f"{c.pump2.get('job') or '-'}  {minutes(c, now)}  "
                f"{last_line(board, c)}"
            )
    return rows


# --- the bot's JSON views: the record shapes of taskpump's
# `status --json` / `pending --json`, so /menu Pump and the chat tools read
# both pumps the same way.

ASK_HEAD = "analyze: questions for the owner"
_COMMENT = re.compile(r"^### @(\S+) — .*$", re.M)
_QUESTION = re.compile(r"^\s*\**(\d+)[.)]\**\s+(.+?)\s*$")


def _held_cards(boards: dict[str, Board], only: str | None) -> list[tuple[Board, Card]]:
    out = []
    for board in boards.values():
        if only and board.name != only:
            continue
        cards, _bad = list_cards(board)
        out += [(board, c) for c in cards if c.pump2.get("stage")]
    return out


def _title(card: Card) -> str:
    return str(card.fm.get("title") or "").strip("\"'")


def question_block(card: Card) -> str:
    """The last ``### @pump2`` comment that asks the owner; '' if none."""
    heads = list(_COMMENT.finditer(card.body))
    block = ""
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(card.body)
        text = card.body[m.end() : end].strip()
        if m.group(1) == "pump2" and text.startswith(ASK_HEAD):
            block = text
    return block


def questions_of(block: str) -> list[dict[str, Any]]:
    """{num, text, default} per numbered line; pump2 has no defaults."""
    return [
        {"num": m.group(1), "text": m.group(2), "default": None}
        for ln in block.splitlines()
        if (m := _QUESTION.match(ln))
    ]


def pending_records(
    boards: dict[str, Board], only: str | None = None
) -> list[dict[str, Any]]:
    recs = []
    for board, c in _held_cards(boards, only):
        if c.fm.get("status") != "waiting_user":
            continue
        block = question_block(c)
        recs.append(
            {
                "id": f"{board.name}:{c.id}",
                "title": _title(c),
                "board": board.name,
                "pump": "asked",
                "kind": "question",
                "phase": str(c.pump2.get("stage")),
                "open": "",
                "questions": questions_of(block),
                "block": block,
                "tags": [f"pump2 {c.pump2.get('stage')}"],
            }
        )
    return recs


def pending_lines(records: list[dict[str, Any]]) -> list[str]:
    if not records:
        return ["no task is waiting for an answer"]
    out: list[str] = []
    for r in records:
        mark = "".join(f"  [{tag}]" for tag in r["tags"])
        out += [
            f"=== {r['id']} — {r['title']}{mark}",
            r["block"] or "(no question block found)",
            "",
        ]
    return out


def status_rows(
    boards: dict[str, Board], only: str | None = None, now: datetime | None = None
) -> list[dict[str, Any]]:
    """One row per card pump2 holds (done cards left out), taskpump's keys."""
    now = now or datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for board in boards.values():
        if only and board.name != only:
            continue
        for path, err in list_cards(board)[1]:
            rows.append(
                {
                    "id": f"{board.name}:{path.name}",
                    "title": f"ERROR {err}",
                    "board": board.name,
                    "pump": "error",
                    "status": "error",
                    "group": "error",
                    "icon": "⚠",
                    "why": f"ERROR {err}",
                    "act": "",
                    "hours": None,
                    "first": False,
                }
            )
    for board, c in _held_cards(boards, only):
        stage = str(c.pump2["stage"])
        if stage == "done":
            continue
        held = c.fm.get("status")
        hours = None
        if held == "waiting_user":
            group, icon, pump, why = "needs", "❓", "asked", "answer questions"
        elif stage == "stopped":
            group, icon, pump, why = "stopped", "⏹", "stopped", "stopped"
        elif c.pump2.get("job"):
            since = _when(c.pump2.get("since"))
            if since:
                hours = round((now - since).total_seconds() / 3600, 1)
            group, icon, pump, why = "running", "▶", "running", f"pump2 {stage}"
        else:
            group, icon, pump, why = "queued", "⏳", "queued", f"pump2 {stage}"
        rows.append(
            {
                "id": f"{board.name}:{c.id}",
                "title": _title(c),
                "board": board.name,
                "pump": pump,
                "status": held,
                "group": group,
                "icon": icon,
                "why": why,
                "act": "",
                "hours": hours,
                "first": False,
            }
        )
    return rows
