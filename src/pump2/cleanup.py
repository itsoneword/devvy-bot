"""Worktree sweep (dv-8bf8): the tick removes the checkout of a finished card.

A card at ``pump2.stage: done`` loses ``.pump-worktrees/<id>/``; a done
designloop card loses its ``<id>-vN`` / ``<id>-current`` copies (the picked
variant only once every card with it in ``deps:`` is done). Removal is plain
``git worktree remove`` (never ``--force``); only pump2's own ``.pump/`` is
discarded. A worktree with any other uncommitted or untracked file is kept and
the card gets one note per distinct file set. Branches are never deleted.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from . import design
from .state import WORKTREES, Board, Card, add_comment, set_keys, set_pump2

PUMP_DIR = ".pump"
NOTE_CAP = 10  # files named in the "kept" note


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120
    )


def _dirty(wt: Path) -> tuple[list[str], bool]:
    """(changed paths outside .pump/, whether .pump/ shows in status).
    Raises RuntimeError when git cannot read the worktree."""
    r = _git(["status", "--porcelain", "--untracked-files=all"], wt)
    if r.returncode:
        lines = (r.stderr or r.stdout).strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"git status rc={r.returncode}")
    files: list[str] = []
    pump_shown = False
    for ln in r.stdout.splitlines():
        path = ln[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path == PUMP_DIR or path.startswith(PUMP_DIR + "/"):
            pump_shown = True
        elif path:
            files.append(path)
    return files, pump_shown


def _remove(board: Board, wt: Path, pump_shown: bool) -> None:
    if pump_shown:  # untracked (not ignored) .pump/ would block a plain remove
        shutil.rmtree(wt / PUMP_DIR, ignore_errors=True)
    r = _git(["worktree", "remove", str(wt)], board.repo)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip())


def _sweep_dirs(
    board: Board, tag: str, wts: list[Path]
) -> tuple[list[str], dict[str, list[str]]]:
    """Remove the clean ones; (tick lines, {worktree name: dirty files})."""
    out: list[str] = []
    kept: dict[str, list[str]] = {}
    for wt in wts:
        try:
            files, pump_shown = _dirty(wt)
            if files:
                kept[wt.name] = files
                continue
            _remove(board, wt, pump_shown)
            out.append(f"{tag} worktree removed {wt.name}")
        except Exception as e:  # one worktree never fails the tick
            out.append(f"{tag} worktree {wt.name} not removed: {e}")
    return out, kept


def _signature(kept: dict[str, list[str]]) -> str:
    text = "\n".join(f"{n}:{f}" for n in sorted(kept) for f in sorted(kept[n]))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _note(kept: dict[str, list[str]]) -> str:
    parts = []
    for name in sorted(kept):
        files = sorted(kept[name])
        shown = ", ".join(files[:NOTE_CAP])
        more = f" (+{len(files) - NOTE_CAP} more)" if len(files) > NOTE_CAP else ""
        parts.append(f"`{WORKTREES}/{name}`: {shown}{more}")
    return "worktree kept: uncommitted " + "; ".join(parts)


def _design_dirs(board: Board, card: Card, cards: list[Card]) -> list[Path]:
    root = board.repo / WORKTREES
    names = {f"{card.id}-{b.rsplit('/', 1)[-1]}" for b in design.design_variants(card)}
    names |= {p.name for p in root.glob(f"{card.id}-v*")}
    names.add(f"{card.id}-current")
    pick = design._fm_str(card, "design_pick")
    if pick:
        waiting = any(
            card.id in [str(d) for d in (c.fm.get("deps") or [])]
            and c.fm.get("status") != "done"
            for c in cards
        )
        if waiting:
            names.discard(f"{card.id}-{pick}")
    return [root / n for n in sorted(names) if (root / n).is_dir()]


def sweep(board: Board, cards: list[Card]) -> list[str]:
    """One pass over the board's done cards; one line per action or failure."""
    out: list[str] = []
    for card in cards:
        tag = f"{board.name}:{card.id}"
        try:
            if card.pump2.get("stage") == "done":
                wts = [board.repo / WORKTREES / card.id]
                key = "pump2"
            elif design.is_designloop(card) and card.fm.get("status") == "done":
                wts = _design_dirs(board, card, cards)
                key = "design"
            else:
                continue
            wts = [w for w in wts if w.is_dir()]
            if not wts:
                continue
            lines, kept = _sweep_dirs(board, tag, wts)
            out += lines
            if not kept:
                continue
            sig = _signature(kept)
            if key == "pump2":
                if card.pump2.get("wt_kept") == sig:
                    continue
                add_comment(card.path, _note(kept))
                set_pump2(card.path, wt_kept=sig)
            else:
                if design._fm_str(card, "design_wt_kept") == sig:
                    continue
                add_comment(card.path, _note(kept))
                set_keys(card.path, design_wt_kept=sig)
            out.append(f"{tag} worktree kept: uncommitted files")
        except Exception as e:  # one card never fails the tick
            out.append(f"{tag} worktree sweep failed: {e}")
    return out
