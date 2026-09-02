#!/usr/bin/env python3
"""Render ``.env`` for this machine (dv-7d92 step 5).

Two jobs, and the split between them is the whole design:

* **Fresh install** -- seed from ``.env.example``, blank out its placeholder
  values (``/path/to/your/projects`` in ``APPROVED_DIRECTORY`` is not a
  harmless default: it puts the bot into allowlist mode against a directory
  that does not exist), apply the installer's real values, generate secrets.
* **Re-run over an existing .env** -- touch as little as possible. Back the
  file up, apply only the explicit ``--set`` values and fill in secrets that
  are still empty. New keys from ``.env.example`` are NOT merged in: every
  field in ``settings.py`` has a default, and quietly appending a hundred lines
  to a config someone has tuned is how an "idempotent" installer loses a
  deployment.

Always ends with mode 600 -- the file holds the bot token and, usually, an
Anthropic key.

Standard library only; runs before ``poetry install``.

Usage:
    render_env.py --set TELEGRAM_BOT_TOKEN=123:abc --set TELEGRAM_BOT_USERNAME=mybot
    render_env.py --set ALLOWED_USERS=4242 --generate-secret AUTH_TOKEN_SECRET
    render_env.py --dry-run --set TELEGRAM_BOT_TOKEN=x
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
# upsert_env is shared with the owner-capture step, which writes into the same
# file moments later (dv-5c83). One implementation of "set a key in a .env,
# keep every comment" is the point -- two would drift.
from capture_owner import upsert_env  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV = REPO_ROOT / ".env"
DEFAULT_EXAMPLE = REPO_ROOT / ".env.example"

# Secrets the installer generates rather than asking anyone to invent.
GENERATED_SECRETS = ("AUTH_TOKEN_SECRET", "WEBHOOK_API_SECRET")

# Values in .env.example that are prose, not configuration. Left as-is they
# either break startup (a nonexistent APPROVED_DIRECTORY raises) or look
# configured when they are not.
PLACEHOLDER_PATTERNS = (
    re.compile(r"^your[_-]", re.IGNORECASE),
    re.compile(r"^/path/to/", re.IGNORECASE),
    re.compile(r"^<.*>$"),
    re.compile(r"^(changeme|replace[_-]?me|xxx+)$", re.IGNORECASE),
)

ASSIGNMENT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def is_placeholder(value: str) -> bool:
    """Is this a documentation stand-in rather than a real value?"""
    stripped = value.strip().strip("'\"")
    if not stripped:
        return False
    return any(p.search(stripped) for p in PLACEHOLDER_PATTERNS)


def read_values(text: str) -> Dict[str, str]:
    """Uncommented assignments in a .env body. Last one wins, as dotenv does."""
    values: Dict[str, str] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = ASSIGNMENT_RE.match(line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def blank_placeholders(text: str) -> Tuple[str, List[str]]:
    """Empty out every placeholder value. Returns the text and what changed."""
    cleared: List[str] = []
    out: List[str] = []
    for line in text.splitlines():
        match = ASSIGNMENT_RE.match(line) if not line.lstrip().startswith("#") else None
        if match and is_placeholder(match.group(2)):
            cleared.append(match.group(1))
            out.append(f"{match.group(1)}=")
        else:
            out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), cleared


def parse_set(pairs: Sequence[str]) -> Dict[str, str]:
    """``KEY=VALUE`` strings into a mapping. The value may contain ``=``."""
    values: Dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--set expects KEY=VALUE, got {pair!r}")
        key, _, value = pair.partition("=")
        key = key.strip()
        if not key or not ASSIGNMENT_RE.match(f"{key}="):
            raise ValueError(f"--set: {key!r} is not a valid variable name")
        values[key] = value
    return values


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def backup_path(env_path: Path, stamp: str) -> Path:
    return env_path.with_suffix(env_path.suffix + f".bak-{stamp}")


def render(
    *,
    existing: Optional[str],
    example: str,
    overrides: Dict[str, str],
    secret_keys: Sequence[str] = GENERATED_SECRETS,
    secret_factory=new_secret,
) -> Tuple[str, List[str]]:
    """Produce the new .env body. Pure: no filesystem, no clock.

    Returns ``(text, notes)`` -- notes are the human-readable lines the
    installer prints, so the summary says what actually changed.
    """
    notes: List[str] = []
    if existing is None:
        text, cleared = blank_placeholders(example)
        notes.append("created from .env.example")
        if cleared:
            notes.append(f"blanked placeholder values: {', '.join(cleared)}")
    else:
        text = existing
        notes.append("updated in place (existing values kept)")

    for key, value in overrides.items():
        text = upsert_env(text, key, value)
    if overrides:
        notes.append(f"set: {', '.join(sorted(overrides))}")

    current = read_values(text)
    generated = []
    for key in secret_keys:
        value = current.get(key, "")
        if not value or is_placeholder(value):
            text = upsert_env(text, key, secret_factory())
            generated.append(key)
    if generated:
        notes.append(f"generated: {', '.join(generated)}")
    return text, notes


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--env", type=Path, default=DEFAULT_ENV)
    p.add_argument("--example", type=Path, default=DEFAULT_EXAMPLE)
    p.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="set a variable (repeatable); overwrites whatever is there",
    )
    p.add_argument(
        "--generate-secret",
        action="append",
        default=[],
        metavar="KEY",
        help=f"generate if empty (default: {', '.join(GENERATED_SECRETS)})",
    )
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        overrides = parse_set(args.set)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    existing: Optional[str] = None
    if args.env.is_file():
        existing = args.env.read_text(encoding="utf-8")
    elif not args.example.is_file():
        print(
            f"ERROR: neither {args.env} nor {args.example} exists;"
            " nothing to render from.",
            file=sys.stderr,
        )
        return 2

    example = args.example.read_text(encoding="utf-8") if args.example.is_file() else ""
    text, notes = render(
        existing=existing,
        example=example,
        overrides=overrides,
        secret_keys=tuple(args.generate_secret) or GENERATED_SECRETS,
    )

    if args.dry_run:
        print(f"--dry-run: would write {args.env} (mode 600)")
        for note in notes:
            print(f"  - {note}")
        return 0

    if text == existing:
        # A no-op re-run must leave no trace. Backing up an identical file just
        # litters the directory with .env.bak-* -- and those backups hold the
        # bot token, so making more of them is not free.
        print(f"{args.env} is already up to date; nothing written.")
        args.env.chmod(0o600)
        return 0

    if existing is not None:
        backup = backup_path(args.env, str(int(time.time())))
        backup.write_text(existing, encoding="utf-8")
        backup.chmod(0o600)
        notes.append(f"previous file backed up to {backup.name}")

    args.env.write_text(text, encoding="utf-8")
    args.env.chmod(0o600)
    print(f"Wrote {args.env} (mode 600)")
    for note in notes:
        print(f"  - {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
