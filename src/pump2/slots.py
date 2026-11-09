"""Heavy-run slots (docs/pump.md section 3): 3 per host, 1 per board.

flock on files in ``locks/``: the kernel frees a slot when its holder dies.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

HOST_SLOTS = 3


def default_dir() -> Path:
    root = os.environ.get("DEVVY_ROOT") or str(Path(__file__).resolve().parents[2])
    return Path(root) / "data" / "pump2" / "locks"


def _try(path: Path) -> IO[str] | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


@contextmanager
def heavy_slot(board: str, locks: Path | None = None) -> Iterator[bool]:
    """Yields True holding one board lock and one host slot, else False."""
    d = locks or default_dir()
    held: list[IO[str]] = []
    try:
        b = _try(d / f"board-{board}.lock")
        if b:
            held.append(b)
            for i in range(HOST_SLOTS):
                s = _try(d / f"host-{i}.lock")
                if s:
                    held.append(s)
                    break
        yield len(held) == 2
    finally:
        for f in held:
            f.close()  # closing drops the flock


def free(board: str, locks: Path | None = None) -> bool:
    with heavy_slot(board, locks) as ok:
        return ok
