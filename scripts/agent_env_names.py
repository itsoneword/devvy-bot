#!/usr/bin/env python3
"""Which env var NAMES an agent CLI would get (dv-2976). Read-only.

Never prints a value. Takes a base environment -- this shell's, or a running
bot's via ``--pid`` (names read from /proc/<pid>/environ) -- applies the
startup scrub the bot does after loading Settings, then shows:

* scrubbed: names the bot removes from its own os.environ
* claude:   names the Claude CLI gets (Agent SDK merge of the scrubbed env)
* codex:    names the Codex CLI gets (allowlist; per-turn vars not shown)

Usage:
    poetry run python scripts/agent_env_names.py
    poetry run python scripts/agent_env_names.py --pid "$(systemctl show -p MainPID --value claude-bot)"

Note: a bot that predates dv-2976 does not scrub yet; this shows what it
WILL pass once running the new code.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import dotenv_values  # noqa: E402

from src.config.settings import Settings  # noqa: E402
from src.utils.agent_env import (  # noqa: E402
    agent_child_env,
    is_secret_name,
    scrub_process_env,
    sdk_child_env_names,
)
from src.utils.paths import data_layout  # noqa: E402


def _proc_env(pid: int) -> dict:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    env = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            name, _, value = item.partition(b"=")
            env[name.decode(errors="replace")] = value.decode(errors="replace")
    return env


def _secret_names(env_file: Path, base: dict) -> set:
    names = {
        name.upper()
        for name, field in Settings.model_fields.items()
        if "SecretStr" in str(field.annotation) or is_secret_name(name)
    }
    secrets_file = data_layout(env=base, dotenv=env_file).bot_secrets_file
    if secrets_file is not None and secrets_file.exists():
        names |= {k for k in dotenv_values(secrets_file) if k}
    return names


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pid", type=int, help="read the base env of this process")
    ap.add_argument("--env-file", type=Path, default=REPO / ".env")
    args = ap.parse_args()

    base = _proc_env(args.pid) if args.pid else dict(os.environ)
    env = dict(base)
    scrubbed = scrub_process_env(_secret_names(args.env_file, base), environ=env)

    def show(title: str, names: list) -> None:
        print(f"{title} ({len(names)}):")
        print("  " + (" ".join(sorted(names)) or "-"))

    show("scrubbed from bot env", scrubbed)
    show("claude CLI (SDK merge)", sdk_child_env_names(environ=env))
    show("codex CLI (allowlist)", list(agent_child_env(environ=env)))
    left = [n for n in sdk_child_env_names(environ=env) if is_secret_name(n)]
    if left:
        show("WARNING secret-looking names still reaching claude", left)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
