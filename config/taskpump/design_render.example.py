"""Example design-loop render: one screen of THIS checkout as PNGs.

Copy into your repo (e.g. scripts/design_render.py), commit it, fill in the
three TODOs, and point the board's ``design.render`` at it (docs/pump.md,
"Design loop"):

  python scripts/design_render.py --worktree {worktree} --out {out} \\
      --screen {screen}

The loop runs it with cwd = a variant's worktree (or the current app's). A
worktree is a fresh checkout: no installed dependencies, no build output, no
untracked config. Build or serve the app from the worktree's own code.

Every run seeds its OWN throwaway DB / data dir: the loop renders 2-3 variants
in parallel, and runs sharing one DB wipe each other's seed data mid-shot.

Writes ``<state>-<theme>.png`` (themes dark and light) into ``--out``, one
fixed viewport for every shot. A state that fails is logged on stderr and left
out (the gate reports it missing). Exit 0 when anything rendered, 1 when
nothing did, 2 for an unknown screen or a bad ``--out``.

Playwright is imported lazily: ``--help`` and ``py_compile`` work without it.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

VIEWPORT = (390, 844)  # CSS px; with SCALE 3 every PNG is 1170x2532
SCALE = 3
THEMES = ("dark", "light")
IDLE_MS = 15000

# TODO 1: screen id (docs/features.yaml) -> {state: URL path that opens it}.
# Open a state by deep link and wait for network idle, never for CSS classes:
# a variant may rename them.
SCREENS: dict[str, dict[str, str]] = {
    "home": {"main": "/", "empty": "/?demo=empty"},
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait(port: int, timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with contextlib.suppress(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        time.sleep(0.2)
    raise RuntimeError(f"app did not listen on port {port}")


@contextlib.contextmanager
def serve(worktree: Path) -> Iterator[str]:
    """Start the app from ``worktree`` on a per-run temp DB; yield its URL."""
    with tempfile.TemporaryDirectory(prefix="design-render-") as tmp:
        port = _free_port()
        env = {
            **os.environ,
            # TODO 2: point your app at a fresh DB / data dir under ``tmp``,
            # never the shared dev DB (parallel renders would clobber it).
            "DATABASE_URL": f"sqlite:///{tmp}/app.db",
            "PORT": str(port),
        }
        # TODO 3: seed the DB and start the server from the worktree's code.
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port)],
            cwd=worktree,
            env=env,
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        try:
            _wait(port)
            yield f"http://127.0.0.1:{port}"
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def shoot(url: str, states: dict[str, str], out: Path) -> int:
    """One PNG per state x theme; returns how many were written."""
    from playwright.sync_api import sync_playwright

    done = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for theme in THEMES:
                ctx: Any = browser.new_context(
                    viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]},
                    device_scale_factor=SCALE,
                    color_scheme=theme,
                )
                for state, path in states.items():
                    page = ctx.new_page()
                    try:
                        page.goto(url + path)
                        page.wait_for_load_state("networkidle", timeout=IDLE_MS)
                        page.screenshot(path=str(out / f"{state}-{theme}.png"))
                        done += 1
                    except Exception as exc:  # noqa: BLE001 - gate reports it
                        print(f"render {state}-{theme}: {exc}", file=sys.stderr)
                    finally:
                        page.close()
                ctx.close()
        finally:
            browser.close()
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description="Render one design-loop screen.")
    ap.add_argument("--worktree", type=Path, default=Path("."))
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--screen", required=True)
    a = ap.parse_args()
    states = SCREENS.get(a.screen)
    if states is None:
        print(f"no render recipe for screen {a.screen!r}", file=sys.stderr)
        return 2
    try:
        a.out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"bad --out {a.out}: {exc}", file=sys.stderr)
        return 2
    with serve(a.worktree.resolve()) as url:
        done = shoot(url, states, a.out)
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
