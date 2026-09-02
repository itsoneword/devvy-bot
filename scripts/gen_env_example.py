#!/usr/bin/env python3
"""Generate ``.env.example`` from ``src/config/settings.py`` (dv-8e04).

``settings.py`` is the source of truth for configuration (see ``CLAUDE.md``).
Before this script, ``.env.example`` was a hand-maintained copy of it and had
drifted badly -- whole groups of settings existed only in code. Now the example
file is generated, and ``--check`` fails CI when the checked-in copy no longer
matches what the model says.

What the output looks like, and why:

* **Almost every variable is commented out.** An uncommented assignment in
  ``.env.example`` becomes a *pinned* value in the ``.env`` the installer
  seeds from it (``scripts/render_env.py``), which would freeze today's
  defaults into every install forever. Commented lines document the default
  without pinning it. Only the handful in ``ACTIVE`` -- the ones a human or
  the installer actually fills in -- are left live.
* **Order and grouping come from ``settings.py`` itself.** Section headings are
  the short single-line comments already in the class body (``# Claude
  settings``, ``# Rate limiting``, ...), so adding a field puts it next to its
  neighbours with no second list to maintain. Long explanatory comments in the
  source are not headings and are ignored -- see ``_is_heading``.
* **Values, types and help text come from the live model**, not from parsing:
  ``Settings.model_fields`` already has the resolved default (including the
  ``DEFAULT_*`` constants) and the ``description=`` text.

Variables read with ``os.getenv`` outside ``Settings`` cannot be discovered
this way; they are listed explicitly in ``EXTRA_SECTIONS``.

Usage:
    python scripts/gen_env_example.py            # write .env.example
    python scripts/gen_env_example.py --check    # exit 1 if it would change
    python scripts/gen_env_example.py --stdout   # print, write nothing
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
import typing
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SETTINGS_PY = REPO_ROOT / "src" / "config" / "settings.py"
DEFAULT_OUTPUT = REPO_ROOT / ".env.example"

WRAP = 76

# Variables left as live assignments. Everything else is commented out so the
# default stays in settings.py. Keep this list short: each entry is a value the
# installer writes or a human is expected to fill in by hand.
ACTIVE = {
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_USERNAME",
    "ALLOWED_USERS",
    "ANTHROPIC_API_KEY",
}

# Extra notes attached to individual variables, for things the field
# description cannot say (cross-file consequences, install-time behaviour).
NOTES: Dict[str, str] = {
    "TELEGRAM_BOT_TOKEN": "install.sh --token writes this for you.",
    "ALLOWED_USERS": (
        "install.sh captures your id (it waits for your /start). An EMPTY "
        "list does not mean 'allow everyone': with no owner and no guest the "
        "bot refuses to start."
    ),
    "ANTHROPIC_API_KEY": (
        "One of the three ways to give the bot an LLM login; the other two "
        "are 'claude auth login' at install time and /auth in Telegram. "
        "Leave empty if the claude CLI is already logged in."
    ),
    "APPROVED_DIRECTORY": (
        "Leave unset for denylist mode (recommended). Setting it to a "
        "directory that does not exist stops the bot from starting."
    ),
    "AUTH_TOKEN_SECRET": "install.sh generates one. openssl rand -hex 32.",
    "WEBHOOK_API_SECRET": "install.sh generates one. openssl rand -hex 32.",
    "PROFILES_CONFIG_PATH": (
        "install.sh points this at config/profiles.yaml. Unset = "
        "single-profile mode."
    ),
}

# Env vars read outside Settings (os.getenv), which model_fields cannot see.
# ``grep -o 'os.getenv("[A-Z_]*"' src scripts`` is how to find new ones.
EXTRA_SECTIONS: List[Tuple[str, List[Tuple[str, str, str]]]] = [
    (
        "Environment selection (read by src/config/loader.py)",
        [
            (
                "ENVIRONMENT",
                "development",
                "development, testing or production. Chooses the defaults in "
                "src/config/environments.py.",
            )
        ],
    ),
    (
        "Channel KB MCP daemon (read by src/channel_intel/config.py)",
        [
            ("CHANNEL_INTEL_HOST", "127.0.0.1", "Bind address for the daemon."),
            ("CHANNEL_INTEL_PORT", "8765", "Bind port."),
            ("CHANNEL_INTEL_DB_PATH", "data/channel_intel.db", "Its SQLite file."),
            (
                "CHANNEL_INTEL_TOKEN",
                "",
                "Bearer token shared by the bot seam and the daemon. "
                "openssl rand -hex 32.",
            ),
        ],
    ),
]

HEADER = """\
# Devvy -- configuration reference
#
# GENERATED FILE. Do not edit by hand: run `make env-example` (which runs
# scripts/gen_env_example.py) after changing src/config/settings.py. CI fails
# if this file and the settings model disagree.
#
# Every variable below is optional and shown with its built-in default,
# EXCEPT the few that are left uncommented. Uncommenting a line pins that
# value for good, so only uncomment what you actually want to change.
#
# ./install.sh writes a real .env from this file. You only need to edit it by
# hand if you are installing manually -- see docs/installation.md.
"""


def _is_heading(comment: str) -> bool:
    """Is this single-line comment a section heading rather than a note?

    Headings in ``settings.py`` are short labels on their own line
    (``# Rate limiting``). Explanatory comments are multi-line, or long, or
    written as sentences -- this only ever sees single-line ones, so length and
    punctuation are what is left to go on.
    """
    text = comment.strip()
    if not text or len(text) > 50:
        return False
    if text.endswith((".", ":", ",")):
        return False
    if text.startswith(("NOTE", "TODO", "FIXME", "XXX")):
        return False
    return True


FIELD_RE = re.compile(r"^    ([a-z_][a-z0-9_]*)\s*:")
COMMENT_RE = re.compile(r"^    #\s?(.*)$")


def parse_sections(source: str) -> Dict[str, str]:
    """Map each field name to the section heading it sits under."""
    sections: Dict[str, str] = {}
    current = "General"
    pending: List[str] = []  # the comment block being accumulated
    for line in source.splitlines():
        comment = COMMENT_RE.match(line)
        if comment:
            pending.append(comment.group(1))
            continue
        # The block ended. A heading counts whether or not a field follows it
        # immediately -- a blank line, or a long explanatory comment, may sit
        # between the heading and the first field it covers.
        if len(pending) == 1 and _is_heading(pending[0]):
            current = pending[0].strip()
        pending = []
        field = FIELD_RE.match(line)
        if field:
            sections[field.group(1)] = current
    return sections


def _unwrap_optional(annotation: Any) -> Any:
    """``Optional[X]`` -> ``X``; ``Annotated[X, ...]`` -> ``X``."""
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:  # type: ignore[comparison-overlap]
        return _unwrap_optional(typing.get_args(annotation)[0])
    if origin is typing.Union or str(origin) == "<class 'types.UnionType'>":
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _unwrap_optional(args[0])
    return annotation


def render_type(annotation: Any) -> str:
    """A short, non-Python description of what may be written on the right."""
    inner = _unwrap_optional(annotation)
    origin = typing.get_origin(inner)
    args = typing.get_args(inner)

    if origin is typing.Literal:  # type: ignore[comparison-overlap]
        return " | ".join(str(a) for a in args)
    if origin in (list, List):
        item = args[0] if args else str
        return "comma-separated ints" if item is int else "comma-separated"
    if origin in (dict, Dict):
        return "comma-separated id:value pairs"
    if inner is bool:
        return "true/false"
    if inner is int:
        return "int"
    if inner is float:
        return "number"
    if inner is Path:
        return "path"
    name = getattr(inner, "__name__", str(inner))
    if name == "SecretStr":
        return "secret"
    return "text" if name == "str" else name


def render_default(value: Any) -> str:
    """The default, written the way it would be written in a .env."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return ",".join(f"{k}:{v}" for k, v in value.items())
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _comment_block(text: str) -> List[str]:
    return [f"# {line}" for line in textwrap.wrap(text, WRAP)]


def render_field(name: str, field: Any) -> List[str]:
    """One variable: its help text, then the assignment (usually commented)."""
    key = name.upper()
    required = field.is_required()
    default = "" if required else render_default(field.default)

    head = field.description or ""
    tag = "required" if required else f"{render_type(field.annotation)}"
    head = f"{head}  [{tag}]" if head else f"[{tag}]"

    out = _comment_block(head)
    note = NOTES.get(key)
    if note:
        out += _comment_block(note)
    prefix = "" if (required or key in ACTIVE) else "# "
    value = "" if key in ACTIVE and not required else default
    out.append(f"{prefix}{key}={value}")
    return out


def build(settings_cls: Any, source: str) -> str:
    """The whole file body."""
    sections = parse_sections(source)
    blocks: List[str] = [HEADER.rstrip("\n")]
    current: Optional[str] = None

    for name, field in settings_cls.model_fields.items():
        section = sections.get(name, "General")
        if section != current:
            current = section
            blocks.append("")
            blocks.append(f"# === {section} ===")
        blocks.append("")
        blocks.extend(render_field(name, field))

    for title, entries in EXTRA_SECTIONS:
        blocks.append("")
        blocks.append(f"# === {title} ===")
        for key, default, help_text in entries:
            blocks.append("")
            blocks.extend(_comment_block(help_text))
            blocks.append(f"# {key}={default}")

    return "\n".join(blocks) + "\n"


def generate() -> str:
    from src.config.settings import Settings  # imported late: needs pydantic

    return build(Settings, SETTINGS_PY.read_text(encoding="utf-8"))


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the file on disk differs from the generated content",
    )
    p.add_argument("--stdout", action="store_true", help="print, write nothing")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    content = generate()

    if args.stdout:
        sys.stdout.write(content)
        return 0

    if args.check:
        existing = args.out.read_text(encoding="utf-8") if args.out.is_file() else ""
        if existing == content:
            print(f"{args.out.name} is up to date with src/config/settings.py")
            return 0
        print(
            f"ERROR: {args.out.name} is out of date with src/config/settings.py.\n"
            "       Run `make env-example` and commit the result.",
            file=sys.stderr,
        )
        return 1

    args.out.write_text(content, encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
