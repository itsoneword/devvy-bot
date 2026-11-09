"""``pump2 migrate``: bring a tree from the old pump (``scripts/taskpump``,
releases 1.5 / 1.6) to pump2. Pure file I/O, safe to rerun.

1. ``config/taskpump/boards.json``: every board gets ``"pump2": true`` and
   loses the keys pump2 does not read (``stages``, ``acceptance``,
   ``mergecheck``, ``intake``; each one is named in the output). ``publish``
   stays: ``scripts/release`` reads it.
   ``name`` / ``repo`` / ``profile`` / ``test`` stay. The repo's own board,
   built in on the old pump, gets a ``pump2_home`` object. A missing file is
   created. The old content is copied to ``boards.json.bak`` before a change.
2. Cards (``.deviz/tasks/*.md`` and ``archive/``) of the home board and every
   board in the file: ``pump:`` and every ``pump_*`` key go (with their
   continuation lines). ``pump_privileged`` stays: the merge-confirmation rule
   (``src/storage/board_policy.py::privileged_merge_reason``) still reads it.
   ``pump2:``, ``status`` and the body stay byte-identical. An open card that
   had more than a bare ``pump: skip`` gets one ``### @pump2`` comment with its
   old state; no card is started (``scripts/pump2 start <board:id>`` does).
3. Reports only: a ``taskpump.timer`` user unit still installed, retired
   ``pump_architect`` / ``pump_acceptance`` rows in ``config/models.yaml``, a
   leftover ``config/taskpump/repos``. docs/upgrading.md gives the commands.

Dry run by default; ``--apply`` writes. A second ``--apply`` changes nothing.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .state import HOME_BOARD, HOME_REPO

OLD_BOARD_KEYS = ("stages", "acceptance", "mergecheck", "intake")
RETIRED_ROUTES = ("pump_architect", "pump_acceptance")
CLOSED = {"done", "closed", "cancelled", "canceled", "wontfix", "archived"}
OLD_KEY = re.compile(r"^(pump(?:_[\w-]+)?)\s*:(.*)$")
# Still read by a live security rule (board_policy.privileged_merge_reason).
KEEP = {"pump_privileged"}
TOP_KEY = re.compile(r"^[A-Za-z_][\w-]*\s*:")


# -- boards.json ------------------------------------------------------------


def _entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("boards", [])
    if isinstance(raw, dict):
        return [v for v in raw.values() if isinstance(v, dict)]
    return [b for b in raw if isinstance(b, dict)] if isinstance(raw, list) else []


def migrate_boards(path: Path) -> tuple[str | None, list[str]]:
    """(new boards.json text or None when unchanged, what changes)."""
    old_text = path.read_text() if path.exists() else None
    data: dict[str, Any] = json.loads(old_text) if old_text is not None else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a JSON object")
    what: list[str] = []
    if old_text is None:
        what.append(f"{path}: create")
        data["boards"] = []
    for b in _entries(data):
        name = b.get("name") or Path(str(b.get("repo") or "")).name or "?"
        dropped = [k for k in OLD_BOARD_KEYS if k in b]
        for k in dropped:
            del b[k]
        if dropped:
            what.append(f"board {name}: drop {', '.join(dropped)} (pump2 reads none)")
        if b.get("pump2") is not True:
            b["pump2"] = True
            what.append(f'board {name}: add "pump2": true')
    if not isinstance(data.get("pump2_home"), dict):
        data["pump2_home"] = {"profile": "devops"}
        what.append(
            f"board {HOME_BOARD}: add pump2_home (profile devops; set pump2_test "
            "to the repo's test command, pump2 doctor checks it)"
        )
    if not what:
        return None, []
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n", what


# -- cards ------------------------------------------------------------------


def task_dirs(path: Path) -> list[tuple[str, Path]]:
    """(board name, tasks dir) for the home board and every board in ``path``
    (flag or not: pre-pump2 boards are exactly the ones to migrate)."""
    out: list[tuple[str, Path]] = [(HOME_BOARD, HOME_REPO / ".deviz" / "tasks")]
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    for b in _entries(data if isinstance(data, dict) else {}):
        if not b.get("repo"):
            continue
        repo = Path(str(b["repo"])).expanduser()
        out.append((str(b.get("name") or repo.name), repo / ".deviz" / "tasks"))
    seen: set[Path] = set()
    uniq = []
    for name, d in out:
        r = d.resolve()
        if r not in seen:
            seen.add(r)
            uniq.append((name, d))
    return uniq


def split_old(text: str) -> tuple[str, dict[str, str]] | None:
    """(text without the old keys, {key: raw value}); None: nothing to do."""
    lines = text.split("\n")
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        return None
    end = lines.index("---", 1)
    kept: list[str] = []
    old: dict[str, str] = {}
    cur = ""
    for ln in lines[1:end]:
        m = OLD_KEY.match(ln)
        if m and m.group(1) not in KEEP:
            cur = m.group(1)
            old[cur] = m.group(2).strip()
            continue
        if cur and not TOP_KEY.match(ln) and ln.startswith((" ", "\t", "- ")):
            old[cur] = (old[cur] + " " + ln.strip()).strip()
            continue
        cur = ""
        kept.append(ln)
    if not old:
        return None
    return "\n".join(["---", *kept, *lines[end:]]), old


def status_of(text: str) -> str:
    m = re.search(r"^status\s*:\s*(\S+)", text.split("\n---", 1)[0], re.M)
    return m.group(1).strip("\"'") if m else ""


def carry_over(old: dict[str, str], full: str) -> str:
    """The ``@pump2`` comment for an open card: its old-pump state."""

    def v(key: str) -> str:
        return old.get(key, "").strip().strip("\"'")

    rows = [f"- pump: {v('pump') or '(none)'}"]
    for key, label in (
        ("pump_branch", "branch"),
        ("pump_phase", "phase"),
        ("pump_open", "open questions"),
        ("pump_sigstop", "stop reason"),
        ("pump_blocked", "blocked"),
        ("pump_failed", "failed"),
    ):
        if v(key):
            rows.append(f"- {label}: {v(key)}")
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"### @pump2 — {stamp}\n"
        "Old pump removed (pump2 migrate); its state on this card, carried over:\n"
        + "\n".join(rows)
        + "\nEarlier answers above stay valid. Not started in pump2: "
        f"`scripts/pump2 start {full}` re-enters it.\n"
    )


def needs_comment(old: dict[str, str]) -> bool:
    """A bare ``pump: skip`` (pump2's old marker) carries nothing to keep."""
    return set(old) != {"pump"} or old["pump"].strip("\"'") != "skip"


def clean(path: Path, board: str) -> tuple[str, str] | None:
    """(new text, one-line description) or None when the card is clean."""
    text = path.read_text()
    res = split_old(text)
    if res is None:
        return None
    new, old = res
    status = status_of(text)
    what = f"{path}: drop {', '.join(old)}"
    if status not in CLOSED and needs_comment(old):
        sep = "" if new.endswith("\n\n") else ("\n" if new.endswith("\n") else "\n\n")
        full = f"{board}:{path.stem}" if board else path.stem
        new = f"{new}{sep}{carry_over(old, full)}"
        what += f" + carry-over comment (status {status or 'none'})"
    return new, what


def _write(p: Path, text: str) -> None:
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(text)
    tmp.replace(p)


# -- reports ----------------------------------------------------------------


def notes() -> list[str]:
    """What migrate leaves to the agent (unit and config edits stay visible)."""
    out: list[str] = []
    unit = Path.home() / ".config/systemd/user/taskpump.timer"
    wants = Path.home() / ".config/systemd/user/timers.target.wants/taskpump.timer"
    if unit.exists() or wants.exists():
        out.append(
            "note: taskpump.timer is still installed: "
            "systemctl --user disable --now taskpump.timer (docs/upgrading.md)"
        )
    models = HOME_REPO / "config" / "models.yaml"
    try:
        rows = [
            r
            for r in RETIRED_ROUTES
            if re.search(rf"^\s*-?\s*{r}\b", models.read_text(), re.M)
        ]
    except OSError:
        rows = []
    if rows:
        out.append(
            f"note: {models} still has retired route rows {', '.join(rows)}: "
            "delete them"
        )
    if (HOME_REPO / "config" / "taskpump" / "repos").exists():
        out.append("note: config/taskpump/repos is no longer read (safe to delete)")
    return out


def run(path: Path, apply: bool) -> int:
    pre = "" if apply else "would "
    try:
        new_boards, what = migrate_boards(path)
    except ValueError as e:
        print(f"pump2 migrate: {e}")
        return 2
    for w in what:
        print(pre + w)
    changed = 0
    for board, d in task_dirs(path):
        if not d.is_dir():
            print(f"skip {board}: no tasks dir {d}")
            continue
        for p in sorted([*d.glob("*.md"), *(d / "archive").glob("*.md")]):
            try:
                res = clean(p, board)
            except (OSError, UnicodeDecodeError) as e:
                print(f"error {p}: {e}")
                continue
            if res is None:
                continue
            new, line = res
            changed += 1
            print(pre + line)
            if apply:
                _write(p, new)
    if apply and new_boards is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        _write(path, new_boards)
    for n in notes():
        print(n)
    board_part = "boards.json " + (
        "unchanged" if new_boards is None else ("changed" if apply else "would change")
    )
    verb = "changed" if apply else "would change (dry run; --apply writes)"
    print(f"{board_part}; {changed} card(s) {verb}")
    return 0
