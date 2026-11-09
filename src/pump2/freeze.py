"""Pump v2 freeze / urgent: the one module that knows what it means.

State: one JSON file, ``data/pump2/freeze.json`` (a sibling of the slot
locks; ``DEVVY_ROOT`` honoured)::

    {"frozen": {"at": iso, "by": who} | null,
     "urgent": {"card": "board:id", "path": file, "at": iso, "by": who} | null}

freeze = no new stage launches on any pump2 board (running jobs finish and
are reaped, merges already launched finish). urgent X = freeze, but card X
still launches; it clears itself once X reaches done / stopped or its file is
gone (the board stays frozen). thaw = both cleared. The tick asks one
question, :func:`may_launch`. An unreadable state file fails closed: nothing
launches and :func:`status_line` reports the error.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .state import Card, now_iso, read_card


def default_path() -> Path:
    root = os.environ.get("DEVVY_ROOT") or str(Path(__file__).resolve().parents[2])
    return Path(root) / "data" / "pump2" / "freeze.json"


@dataclass
class State:
    frozen: dict[str, Any] | None = None
    urgent: dict[str, Any] | None = None
    error: str = ""  # the file exists but cannot be read: fail closed


def _p(path: Path | None) -> Path:
    return path or default_path()


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield  # closing the file drops the flock


def read(path: Path | None = None) -> State:
    p = _p(path)
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError:
        return State()
    except (OSError, ValueError) as e:
        return State(error=f"{p}: {e}")
    if not isinstance(data, dict):
        return State(error=f"{p}: not a JSON object")
    fr, ur = data.get("frozen"), data.get("urgent")
    return State(
        frozen=fr if isinstance(fr, dict) else None,
        urgent=ur if isinstance(ur, dict) else None,
    )


def _write(p: Path, st: State) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"frozen": st.frozen, "urgent": st.urgent}, indent=1))
    os.replace(tmp, p)


def freeze(by: str, path: Path | None = None) -> State:
    p = _p(path)
    with _locked(p):
        st = read(p)
        if st.error:
            st = State()  # an explicit freeze rewrites a broken file
        if not st.frozen:
            st.frozen = {"at": now_iso(), "by": by}
        _write(p, st)
    return st


def thaw(by: str, path: Path | None = None) -> State:
    p = _p(path)
    with _locked(p):
        st = State()
        _write(p, st)
    return st


def urgent(card_ref: str, card_path: Path, by: str, path: Path | None = None) -> State:
    """Freeze (if not yet) and let only ``card_ref`` launch."""
    p = _p(path)
    with _locked(p):
        st = read(p)
        if st.error:
            st = State()
        if not st.frozen:
            st.frozen = {"at": now_iso(), "by": by}
        st.urgent = {
            "card": card_ref,
            "path": str(Path(card_path).resolve()),
            "at": now_iso(),
            "by": by,
        }
        _write(p, st)
    return st


def _urgent_over(urg: dict[str, Any]) -> bool:
    try:
        c = read_card(Path(str(urg.get("path") or "")))
    except (OSError, ValueError):
        return True  # gone or unreadable: nothing left to rush
    return c.pump2.get("stage") in ("done", "stopped")


def _settle(p: Path) -> State:
    """The state, with a finished urgent card dropped (lazily, here only)."""
    st = read(p)
    if st.error or not st.urgent or not _urgent_over(st.urgent):
        return st
    with _locked(p):
        st = read(p)
        if not st.error and st.urgent and _urgent_over(st.urgent):
            st.urgent = None
            _write(p, st)
    return st


def may_launch(card: Card, path: Path | None = None) -> bool:
    st = _settle(_p(path))
    if st.error:
        return False
    if not st.frozen:
        return True
    if st.urgent:
        return str(Path(card.path).resolve()) == st.urgent.get("path")
    return False


def status_line(path: Path | None = None) -> str | None:
    """``FROZEN since <iso> by <who>[ · urgent: <board:id>]``, or None."""
    st = _settle(_p(path))
    if st.error:
        return f"FROZEN (state error, nothing launches): {st.error}"
    if not st.frozen:
        return None
    line = f"FROZEN since {st.frozen.get('at', '?')} by {st.frozen.get('by', '?')}"
    if st.urgent:
        line += f" · urgent: {st.urgent.get('card', '?')}"
    return line
