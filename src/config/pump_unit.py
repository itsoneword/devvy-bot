"""The Pump caps, edited in place in the taskpump user unit (dv-6104 phase 3).

The Pump's knobs live in ``Environment=`` lines of the installed user unit
``~/.config/systemd/user/taskpump.service`` — hand-edited, no installer
re-renders it, no drop-in (analysis dv-6104 §2). That file is the one source:
this module reads and rewrites it and nothing else. ``/menu → Настройки``
(``src/bot/orchestration/admin_menu.py``) is its only caller.

* ``read_values`` refuses (``UnitError`` → "edit by hand") when the file is
  missing, an editable value is not an int, or a name is set twice;
* ``check`` is the allowed range per value (owner answer 3.1). bgjob slots
  are not a knob here any more (dv-f6af): ``scripts/taskpump`` hands each
  ``bgjob run`` max(BGJOB_MAX_CONCURRENT, Pump parallel + 1) itself;
* ``write_value`` rewrites only that one line (or appends one after the last
  live ``Environment=`` line, or drops the line it once appended — undo),
  keeps comments, and replaces the file atomically. The caller then runs
  ``systemctl --user daemon-reload``; the timer's next oneshot tick reads it.

Pure, synchronous, standard library only: the read-check-write runs with no
``await`` in between, so two taps in the bot's event loop cannot interleave.
"""

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_UNIT = "~/.config/systemd/user/taskpump.service"


@dataclass(frozen=True)
class Setting:
    name: str
    label: str
    lo: int
    hi: int
    #: The value the script uses when the unit does not set it. Must equal the
    #: ``os.environ.get(NAME, "<n>")`` default in ``script`` — a test checks.
    default: int
    script: str


PUMP = "PUMP_MAX_CONCURRENT"
STALL_CAP = "PUMP_STALL_CAP"

EDITABLE: Dict[str, Setting] = {
    s.name: s
    for s in (
        Setting(PUMP, "Параллельно задач", 1, 6, 1, "taskpump"),
        Setting(STALL_CAP, "Запусков без прогресса", 4, 40, 12, "taskpump"),
    )
}


class UnitError(Exception):
    """The unit cannot be edited safely: the owner edits it by hand."""


def unit_path() -> Path:
    return Path(os.environ.get("TASKPUMP_UNIT_PATH") or DEFAULT_UNIT).expanduser()


def _live_env(line: str) -> Optional[str]:
    """The text after ``Environment=`` of a live (uncommented) line."""
    stripped = line.strip()
    if stripped.startswith("Environment="):
        return stripped[len("Environment=") :]
    return None


def _find(lines: List[str], name: str) -> List[int]:
    """Indexes of the live ``Environment=NAME=…`` lines. A name hidden inside
    a quoted or multi-assignment line is not ours to rewrite → UnitError."""
    found = []
    for i, line in enumerate(lines):
        rest = _live_env(line)
        if rest is None:
            continue
        if rest.startswith(f"{name}="):
            found.append(i)
        elif re.search(rf"(^|[\s\"']){name}=", rest):
            raise UnitError(f"{name} задан в сложной строке Environment=")
    return found


def read_values(path: Path) -> Dict[str, Optional[int]]:
    """The editable values set in the unit (None = unset → script default)."""
    if not path.is_file():
        raise UnitError(f"нет файла {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise UnitError(f"не читается {path}: {exc}") from exc
    values: Dict[str, Optional[int]] = {}
    for name in EDITABLE:
        idx = _find(lines, name)
        if len(idx) > 1:
            raise UnitError(f"{name} задан {len(idx)} раза")
        if not idx:
            values[name] = None
            continue
        raw = (_live_env(lines[idx[0]]) or "").split("=", 1)[1].strip()
        if not re.fullmatch(r"\d+", raw):
            raise UnitError(f"{name}={raw} — не целое число")
        values[name] = int(raw)
    return values


def effective(values: Dict[str, Optional[int]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for name, s in EDITABLE.items():
        value = values.get(name)
        out[name] = s.default if value is None else value
    return out


def check(name: str, new: int, values: Dict[str, Optional[int]]) -> Optional[str]:
    """Why ``name = new`` is refused, or None when it is allowed."""
    s = EDITABLE[name]
    if not s.lo <= new <= s.hi:
        return f"{s.label}: допустимо {s.lo}–{s.hi}, {new} нельзя"
    return None


def write_value(path: Path, name: str, value: Optional[int]) -> None:
    """Set ``name`` to ``value`` in the unit (None = drop the line), keeping
    every other byte. Atomic: a temp file in the same dir, then os.replace."""
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    lines = text.splitlines(keepends=True)
    idx = _find([ln.rstrip("\r\n") for ln in lines], name)
    if len(idx) > 1:
        raise UnitError(f"{name} задан {len(idx)} раза")
    if idx:
        i = idx[0]
        if value is None:
            del lines[i]
        else:
            ending = lines[i][len(lines[i].rstrip("\r\n")) :]
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            lines[i] = f"{indent}Environment={name}={value}{ending}"
    elif value is not None:
        live = [i for i, ln in enumerate(lines) if _live_env(ln) is not None]
        if not live:
            raise UnitError("в файле нет ни одной строки Environment=")
        at = live[-1]
        if not lines[at].endswith("\n"):
            lines[at] += "\n"
        lines.insert(at + 1, f"Environment={name}={value}\n")
    new_text = "".join(lines)
    if new_text == text:
        return
    mode = path.stat().st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.tmp-")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
