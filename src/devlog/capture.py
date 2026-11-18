"""A screenshot of one of our own screens for an interface post (dv-fc9d).

Two existing renderers, both drawing demo data, never live users:

* ``bot``: a screen id of the bot screens registry (``src/bot/screens``),
  drawn offline by ``render.render``;
* ``gymbro``: a screen of gymbro's ``docs/features.yaml``, drawn by the board's
  ``design.render`` argv (``config/taskpump/boards.json``) with ``--lang ru``,
  a subprocess of the bot (not the bash box) with a timeout.

The screen id is checked against the known list before any command is built,
so no free-form argument reaches a command line.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[2]
BOARDS = REPO / "config" / "taskpump" / "boards.json"
SOURCES = ("bot", "gymbro")
GYMBRO_TIMEOUT_S = 240
_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class Screenshot:
    path: Optional[Path]
    error: Optional[str] = None


def _fail(msg: str) -> Screenshot:
    return Screenshot(None, msg)


def bot_screens() -> List[str]:
    from ..bot.screens.registry import SCREENS

    return sorted(SCREENS)


def _gymbro_board() -> Optional[Dict[str, Any]]:
    from ..utils.boards import read_board_entries

    try:
        boards = read_board_entries(BOARDS)
    except (OSError, ValueError):
        return None
    for board in boards:
        if not isinstance(board, dict):
            continue
        if board.get("name") == "gymbro" and (board.get("design") or {}).get("render"):
            return dict(board)
    return None


def gymbro_screens(repo: Optional[Path] = None) -> List[str]:
    """Top-level keys of ``screens:`` in gymbro's ``docs/features.yaml``."""
    if repo is None:
        board = _gymbro_board()
        if board is None:
            return []
        repo = Path(board["repo"])
    try:
        text = (repo / "docs" / "features.yaml").read_text(encoding="utf-8")
    except OSError:
        return []
    out: List[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("screens:"):
            inside = True
            continue
        if inside and line and not line[0].isspace() and not line.startswith("#"):
            break
        m = re.match(r"^  ([a-z0-9_]+):\s*(?:#.*)?$", line) if inside else None
        if m:
            out.append(m.group(1))
    return out


def _out_path(out_dir: Path, source: str, screen: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return out_dir / f"screen-{source}-{screen.replace('.', '_')}-{stamp}.png"


def _render_bot(screen: str, out_dir: Path) -> Screenshot:
    from ..bot.screens.registry import SCREENS
    from ..bot.screens.render import render

    async def _build() -> List[Any]:
        return list(await SCREENS[screen].build())

    try:
        png = render(asyncio.run(_build())[0])
    except Exception as exc:  # a screen that cannot be drawn
        return _fail(f"render failed: {type(exc).__name__}: {exc}")
    path = _out_path(out_dir, "bot", screen)
    path.write_bytes(png)
    return Screenshot(path)


def _render_gymbro(screen: str, out_dir: Path, board: Dict[str, Any]) -> Screenshot:
    with tempfile.TemporaryDirectory(prefix="devlog-shot-") as tmp:
        argv = [
            screen if a == "{screen}" else tmp if a == "{out}" else str(a)
            for a in board["design"]["render"]
        ]
        if "--lang" in argv:
            argv[argv.index("--lang") + 1] = "ru"
        try:
            proc = subprocess.run(
                argv,
                cwd=board["repo"],
                capture_output=True,
                text=True,
                timeout=GYMBRO_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _fail(f"gymbro render timed out after {GYMBRO_TIMEOUT_S}s")
        except OSError as exc:
            return _fail(f"gymbro render could not start: {exc}")
        shots = sorted(Path(tmp).glob("*.png"))
        if proc.returncode != 0 or not shots:
            tail = (proc.stderr or proc.stdout or "").strip()[-300:]
            return _fail(f"gymbro render failed (exit {proc.returncode}): {tail}")
        light = [p for p in shots if p.stem.endswith("-light")]
        path = _out_path(out_dir, "gymbro", screen)
        path.write_bytes((light or shots)[0].read_bytes())
        return Screenshot(path)


def render_screenshot(
    source: str, screen: str, out_dir: Optional[Path] = None
) -> Screenshot:
    """A PNG of one known screen, or an error and no file."""
    out_dir = out_dir or Path(tempfile.gettempdir()) / "devlog"
    if source not in SOURCES:
        return _fail(f"source must be one of {', '.join(SOURCES)}")
    if not isinstance(screen, str) or not _ID.match(screen):
        return _fail("unknown screen id")
    if source == "bot":
        if screen not in bot_screens():
            return _fail(f"unknown bot screen {screen!r}")
        return _render_bot(screen, out_dir)
    board = _gymbro_board()
    if board is None:
        return _fail("the gymbro board has no design.render command")
    if screen not in gymbro_screens(Path(board["repo"])):
        return _fail(f"unknown gymbro screen {screen!r}")
    return _render_gymbro(screen, out_dir, board)
