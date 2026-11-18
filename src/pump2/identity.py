"""Who this install is on a board: the one place for the identity.

Two levels: the *install* (the agent, e.g. ``devvybot-vps``) and its
*profiles* (the install's sub-agents: devops, builder, coach; plus the
code-fixed pseudo-profiles ``pump2``, ``owner``, ``cli``). An author is
``<install>/<profile>``, stamped by code: the install part comes only from
here, never from a tool, CLI or HTTP argument.

The identity is the top-level ``me`` (+ optional ``me_aliases``) of the
boards file (``config/taskpump/boards.json``). ``me`` unset = single-install
mode: every card is ours, authors use the fallback ``devvybot-<hostname>``.

Ownership is per install: a card is ours when its assignee is empty, ``me``,
``<me>/<any profile>``, or a bare name that is a local profile (a profiles
registry slug, a board's ``profile``) or a ``me_aliases`` entry.
"""

from __future__ import annotations

import json
import os
import re
import socket
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import yaml

HOME_REPO = Path(__file__).resolve().parents[2]
DEFAULT_BOARDS_FILE = HOME_REPO / "config" / "taskpump" / "boards.json"
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
# a profile part: a slug, optionally a guest suffix (`coach#u1`)
PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]*(?:#[A-Za-z0-9_-]+)?")
PSEUDO = ("pump2", "owner", "cli")


@dataclass(frozen=True)
class Identity:
    me: str = ""  # "" = not set: single-install mode
    aliases: frozenset[str] = frozenset()
    # the `profile` of every board in the boards file
    board_profiles: frozenset[str] = field(default_factory=frozenset)

    @property
    def install(self) -> str:
        """The install part of an author: ``me``, else the host fallback."""
        return self.me or fallback_install()


def fallback_install() -> str:
    host = re.sub(r"[^a-z0-9-]+", "-", socket.gethostname().lower()).strip("-")
    return f"devvybot-{host or 'local'}"


def valid_name(name: str) -> bool:
    """An install name: ``[a-z0-9][a-z0-9-]*`` (no ``/``, space, ``#``)."""
    return bool(NAME.fullmatch(name or ""))


def valid_assignee(value: str) -> bool:
    """``<install>`` or ``<install>/<profile>``, each part a valid name."""
    parts = (value or "").split("/")
    return len(parts) <= 2 and all(valid_name(p) for p in parts)


def load(path: Path | None = None) -> Identity:
    """The identity in a boards file; a missing / unreadable file or a bad
    ``me`` = unset (never raises: the tick must not die on it)."""
    path = path or DEFAULT_BOARDS_FILE
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return Identity()
    if not isinstance(data, dict):
        return Identity()
    me = data.get("me")
    me = me.strip() if isinstance(me, str) and valid_name(me.strip()) else ""
    raw = data.get("me_aliases")
    aliases = (
        frozenset(str(x).strip() for x in raw if isinstance(x, str) and x.strip())
        if isinstance(raw, list)
        else frozenset()
    )
    profiles: set[str] = set()
    home = data.get("pump2_home")
    if isinstance(home, dict):
        profiles.add(str(home.get("profile") or "devops"))
    entries = data.get("boards", [])
    if isinstance(entries, dict):
        entries = list(entries.values())
    for b in entries if isinstance(entries, list) else []:
        if isinstance(b, dict):
            profiles.add(str(b.get("profile") or "devops"))
    return Identity(me=me, aliases=aliases, board_profiles=frozenset(profiles))


_current: Identity | None = None


def current() -> Identity:
    """The identity in force: the one ``using`` set (the CLI's ``--boards``,
    the tick's ``boards_file``), else ``PUMP2_BOARDS_FILE``, else the default
    boards file."""
    if _current is not None:
        return _current
    env = os.environ.get("PUMP2_BOARDS_FILE")
    return load(Path(env) if env else None)


@contextmanager
def using(path: Path | None) -> Iterator[Identity]:
    """Run a block with the identity of boards file ``path`` (None = default)."""
    global _current
    prev = _current
    _current = load(path)
    try:
        yield _current
    finally:
        _current = prev


def registry_slugs() -> frozenset[str]:
    """The profile slugs of the bot's profiles registry
    (``PROFILES_CONFIG_PATH``, else ``config/profiles.yaml``); read as plain
    YAML (no bot config load: that logs to stdout). Missing = none."""
    env = os.environ.get("PROFILES_CONFIG_PATH")
    path = Path(env) if env else HOME_REPO / "config" / "profiles.yaml"
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return frozenset()
    items = data.get("profiles") if isinstance(data, dict) else data
    if isinstance(items, dict):
        return frozenset(str(k) for k in items)
    if not isinstance(items, list):
        return frozenset()
    return frozenset(
        str(i["slug"]) for i in items if isinstance(i, dict) and i.get("slug")
    )


def local_profiles(
    ident: Identity | None = None, extra: Iterable[str] = ()
) -> frozenset[str]:
    """Every bare name that is a profile of this install."""
    ident = ident or current()
    return frozenset(ident.board_profiles) | registry_slugs() | frozenset(extra)


def _bare(name: str) -> str:
    return name.split("#", 1)[0].strip()


def is_ours(
    assignee: object,
    *,
    me: str,
    local: Iterable[str] = (),
    aliases: Iterable[str] = (),
) -> bool:
    """The one ownership rule (install level only). ``me`` empty = every
    card is ours (single-install mode)."""
    a = str(assignee or "").strip().strip("\"'")
    if not me or not a:
        return True
    if "/" in a:
        return a.split("/", 1)[0] == me
    a = _bare(a)
    return a == me or a in set(local) or a in set(aliases)


def ours_check(
    ident: Identity | None = None, extra: Iterable[str] = ()
) -> Callable[[object], bool]:
    """``is_ours`` bound to this install's identity and local profiles
    (read once): ``check(assignee) -> bool``. ``extra``: more local profile
    names (the served boards' ``profile``)."""
    ident = ident or current()
    if not ident.me:
        return lambda _a: True
    me, local, aliases = ident.me, local_profiles(ident, extra), ident.aliases
    return lambda a: is_ours(a, me=me, local=local, aliases=aliases)


def card_is_ours(
    assignee: object, ident: Identity | None = None, extra: Iterable[str] = ()
) -> bool:
    return ours_check(ident, extra)(assignee)


def owner_of(fm: dict[str, object]) -> str:
    """The card's assignee as written ('' = none)."""
    return str(fm.get("assignee") or "").strip().strip("\"'")


def profile_of(author: object) -> str:
    """The profile part of an author (``<install>/<profile>`` or a legacy
    bare slug), guest suffix kept."""
    a = str(author or "").strip()
    return a.split("/", 1)[1] if "/" in a else a


def author(profile: str, *, me: str | None = None) -> str:
    """``<install>/<profile>``. ``profile`` is set by the calling code; one
    with ``/``, not a slug, or equal to the install name is refused, so no
    caller can write another install's name."""
    install = (current().install if me is None else me) or fallback_install()
    p = (profile or "").strip()
    if not PROFILE.fullmatch(p) or _bare(p) == install:
        raise ValueError(f"not a profile name: {profile!r}")
    return f"{install}/{p}"
