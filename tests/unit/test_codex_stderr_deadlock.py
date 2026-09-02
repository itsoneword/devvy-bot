"""Codex CLI subprocess plumbing (2026-08-30 live hang).

A real turn hung for the full 600s agent timeout and left an orphan running
30+ minutes after. Two independent defects, both reproduced here against real
subprocesses rather than mocks — a mock cannot fill a 64KiB pipe buffer, which
is the entire mechanism.
"""

import asyncio
import os
import signal

import pytest

from src.agent_backends.codex_cli import STREAM_LIMIT, _terminate

PIPE_BUF = 65536


async def _spawn(script: str, drain_stderr: bool) -> tuple[bool, list[str]]:
    """Run ``script``, read stdout to EOF, optionally draining stderr too.

    Returns (completed_within_timeout, stdout_lines).
    """
    proc = await asyncio.create_subprocess_exec(
        "python3",
        "-c",
        script,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None and proc.stderr is not None

    async def _drain() -> None:
        """Chunked, exactly like the production drainer.

        readline() here is what made this helper flaky: the child writes one
        line far over StreamReader's 64KiB limit, so readline() raises and the
        drainer dies — sometimes after freeing enough buffer for the child to
        finish, sometimes not.
        """
        assert proc.stderr is not None
        while await proc.stderr.read(65536):
            pass

    drainer = asyncio.create_task(_drain()) if drain_stderr else None
    lines: list[str] = []
    try:

        async def _read_all() -> None:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                lines.append(line.decode().strip())

        await asyncio.wait_for(_read_all(), timeout=5)
        await asyncio.wait_for(proc.wait(), timeout=5)
        return True, lines
    except asyncio.TimeoutError:
        return False, lines
    finally:
        if drainer is not None:
            drainer.cancel()
        await _terminate(proc)


# A child that writes far more to stderr than the 64KiB pipe holds, then
# finishes on stdout. This is exactly what codex does: a models-list parse
# failure dumps the whole ~250KB response body to stderr, twice per turn.
NOISY_CHILD = f"""
import sys
sys.stdout.write('{{"type":"turn.started"}}\\n'); sys.stdout.flush()
sys.stderr.write("E" * {PIPE_BUF * 4} + "\\n"); sys.stderr.flush()
sys.stdout.write('{{"type":"turn.completed"}}\\n'); sys.stdout.flush()
"""


async def test_undrained_stderr_deadlocks_the_turn():
    """The bug: reading only stdout blocks the child mid-write to stderr."""
    completed, lines = await _spawn(NOISY_CHILD, drain_stderr=False)

    assert completed is False, "expected the deadlock this test exists to pin"
    # It got the first event, then the child blocked before the second.
    assert lines == ['{"type":"turn.started"}']


async def test_draining_stderr_lets_the_turn_finish():
    """The fix: a concurrent drainer keeps the pipe from filling."""
    completed, lines = await _spawn(NOISY_CHILD, drain_stderr=True)

    assert completed is True
    assert lines == ['{"type":"turn.started"}', '{"type":"turn.completed"}']


async def test_terminate_kills_a_grandchild_not_just_the_direct_child():
    """`codex` is a Node shim around a vendored binary; killing the shim
    orphaned the binary for 30+ minutes on 2026-08-30."""
    shim = (
        "import subprocess,sys,time\n"
        "c = subprocess.Popen(['sleep','300'])\n"
        "sys.stdout.write(str(c.pid)+'\\n'); sys.stdout.flush()\n"
        "time.sleep(300)\n"
    )
    proc = await asyncio.create_subprocess_exec(
        "python3",
        "-c",
        shim,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.stdout is not None
    grandchild = int((await asyncio.wait_for(proc.stdout.readline(), 10)).decode())

    os.kill(grandchild, 0)  # alive before
    await _terminate(proc)
    await asyncio.sleep(0.3)

    try:
        os.kill(grandchild, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    if alive:  # pragma: no cover - only on failure, so the box stays clean
        os.kill(grandchild, signal.SIGKILL)
    assert alive is False, "grandchild survived; the orphan bug is back"


async def test_terminate_is_safe_on_an_already_dead_process():
    proc = await asyncio.create_subprocess_exec(
        "true", stdout=asyncio.subprocess.DEVNULL, start_new_session=True
    )
    await proc.wait()
    await _terminate(proc)  # must not raise


# --- the case the first fix missed --------------------------------------

# codex's models-parse error is ONE line of ~250KB. asyncio's StreamReader
# defaults to a 64KiB line limit and readline() RAISES on a longer line rather
# than returning it -- so a readline()-based drainer dies on the first such
# line and the pipe fills anyway. This child reproduces exactly that shape.
ONE_HUGE_LINE = f"""
import sys
sys.stdout.write('{{"type":"turn.started"}}\\n'); sys.stdout.flush()
sys.stderr.write("E" * {PIPE_BUF * 4} + "\\n"); sys.stderr.flush()
sys.stdout.write('{{"type":"turn.completed"}}\\n'); sys.stdout.flush()
"""


async def _drain_with(proc, use_readline: bool):
    """Drain stderr either way, reporting whether the drainer survived."""
    died = []

    async def _run() -> None:
        try:
            while True:
                if use_readline:
                    chunk = await proc.stderr.readline()
                else:
                    chunk = await proc.stderr.read(65536)
                if not chunk:
                    return
        except Exception as exc:
            died.append(exc)

    return asyncio.create_task(_run()), died


async def test_readline_drainer_dies_on_a_line_over_the_limit():
    """Why the first fix did not work: the drainer raised and stopped."""
    proc = await asyncio.create_subprocess_exec(
        "python3",
        "-c",
        ONE_HUGE_LINE,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    task, died = await _drain_with(proc, use_readline=True)
    await asyncio.sleep(1.0)
    task.cancel()
    await _terminate(proc)

    assert died, "expected readline() to raise on a >64KiB line"


async def test_chunked_drainer_survives_a_line_over_the_limit():
    """The actual fix: fixed-size reads have no line-length limit."""
    proc = await asyncio.create_subprocess_exec(
        "python3",
        "-c",
        ONE_HUGE_LINE,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    task, died = await _drain_with(proc, use_readline=False)
    try:
        lines = []

        async def _read_all():
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                lines.append(line.decode().strip())

        await asyncio.wait_for(_read_all(), timeout=5)
        await asyncio.wait_for(proc.wait(), timeout=5)
    finally:
        task.cancel()
        await _terminate(proc)

    assert not died
    assert lines == ['{"type":"turn.started"}', '{"type":"turn.completed"}']


async def test_stdout_readline_survives_a_huge_json_event():
    """A single large JSON event must not kill the turn either.

    Same trap on the other stream: without a raised `limit`,
    proc.stdout.readline() raises on an oversized event line instead of
    returning it. The payload is built inside the child — passing it as an
    argument hits ARG_MAX ("Argument list too long").
    """
    script = (
        "import sys\n"
        f"big = 'x' * {PIPE_BUF * 3}\n"
        'sys.stdout.write(\'{"type":"item.completed","text":"\' '
        "+ big + '\"}' + '\\n')\n"
        "sys.stdout.flush()\n"
    )
    proc = await asyncio.create_subprocess_exec(
        "python3",
        "-c",
        script,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        limit=STREAM_LIMIT,
    )
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
    await _terminate(proc)

    assert len(line) > PIPE_BUF * 3
    assert line.startswith(b'{"type":"item.completed"')


async def test_stdout_readline_raises_without_the_raised_limit():
    """Pins WHY the limit= argument is load-bearing, not decoration."""
    script = (
        "import sys\n"
        f"big = 'x' * {PIPE_BUF * 3}\n"
        "sys.stdout.write(big + '\\n'); sys.stdout.flush()\n"
    )
    proc = await asyncio.create_subprocess_exec(
        "python3",
        "-c",
        script,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        with pytest.raises(ValueError):
            await asyncio.wait_for(proc.stdout.readline(), timeout=10)
    finally:
        await _terminate(proc)
