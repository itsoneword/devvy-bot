"""Hard stop for a Claude CLI process and everything it spawned (dv-b399).

``client.interrupt()`` is a polite control request the CLI only answers at a
stream boundary; a CLI blocked in a tool (or wedged) never answers, and the
SDK's ``disconnect()`` only ends the CLI itself, leaving the shells it started
(and what they started) running. This module ends the whole tree.

The SDK launches the CLI without its own session, so there is no process group
to signal; the tree is found by walking parent pids in ``/proc``.

Detached background jobs are NOT in the tree: ``bgjob`` starts them with
``systemd-run --user`` (units owned by the user manager, not by the CLI), so
they keep running when a turn is stopped. That is the owner's rule.
"""

import asyncio
import os
import signal
from typing import Dict, List, Optional

import structlog

logger = structlog.get_logger()

_PROC = "/proc"


def _parent_map() -> Dict[int, int]:
    """pid -> ppid for every process we can read."""
    parents: Dict[int, int] = {}
    try:
        names = os.listdir(_PROC)
    except OSError:
        return parents
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"{_PROC}/{name}/stat", "rb") as fh:
                data = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        # "pid (comm) S ppid ..." -- comm may hold spaces and parentheses.
        tail = data.rsplit(")", 1)[-1].split()
        if len(tail) >= 2 and tail[1].isdigit():
            parents[int(name)] = int(tail[1])
    return parents


def descendants(pid: int) -> List[int]:
    """All transitive children of ``pid``, deepest first (``pid`` excluded)."""
    parents = _parent_map()
    children: Dict[int, List[int]] = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)
    ordered: List[int] = []
    stack = list(children.get(pid, ()))
    while stack:
        cur = stack.pop()
        ordered.append(cur)
        stack.extend(children.get(cur, ()))
    ordered.reverse()  # children before their parents
    return ordered


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _alive(pid: int) -> bool:
    try:
        with open(f"{_PROC}/{pid}/stat", "rb") as fh:
            state = fh.read().decode("utf-8", "replace").rsplit(")", 1)[-1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def cli_pid(client: object) -> Optional[int]:
    """Pid of the CLI behind a ``ClaudeSDKClient`` (SDK-private attributes)."""
    process = getattr(getattr(client, "_transport", None), "_process", None)
    pid = getattr(process, "pid", None)
    return pid if isinstance(pid, int) and pid > 1 else None


async def terminate_tree(pid: int, *, grace: float = 2.0) -> int:
    """SIGTERM then SIGKILL ``pid`` and all its descendants. Returns how many
    processes were signalled.

    Refuses anything that is not a direct child of this process, so a stale or
    recycled pid can never reach an unrelated process.
    """
    if pid == os.getpid() or _parent_map().get(pid) != os.getpid():
        logger.warning("Refusing to stop a pid that is not our child", pid=pid)
        return 0
    victims = descendants(pid) + [pid]
    for victim in victims:
        _signal(victim, signal.SIGTERM)
    deadline = asyncio.get_running_loop().time() + grace
    while asyncio.get_running_loop().time() < deadline:
        if not any(_alive(v) for v in victims):
            break
        await asyncio.sleep(0.05)
    # Anything that survived, plus children spawned while we were waiting.
    survivors = [v for v in victims if _alive(v)]
    for extra in descendants(pid):
        if extra not in survivors:
            survivors.append(extra)
    for victim in survivors:
        _signal(victim, signal.SIGKILL)
    return len(victims)
