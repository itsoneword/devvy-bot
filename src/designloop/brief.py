"""The design brief, assembled by script from the board repo's docs (dv-9133).

Never written by hand: a hand-written brief prescribed the answer (three
fixed variants) and lost features (owner, designer audit 2026-10-27). The
sources, all in the board repo (gymbro today):

- ``docs/features.yaml``: must-keep, every feature / action / API route of
  the screen. A feature may move, merge or go behind a tap; it may not be lost.
- ``docs/SCREENS.md``: the screen's ``###`` passport and the §1 shared tokens.
- ``docs/screens/<screen_id>/<state>-<theme>.png``: the current screen, the
  "before" the critic always sees next to a variant.
- ``docs/UI_GUIDE.md``: the usability rules.

Pure file reading; the brief states the problem, never the variants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

FEATURES_PATH = "docs/features.yaml"
PASSPORTS_PATH = "docs/SCREENS.md"
REFS_DIR = "docs/screens"
GUIDE_PATH = "docs/UI_GUIDE.md"
THEMES = ("dark", "light")

# Owner 27.10 (designer audit): the biggest lever. Who needs a feature, how
# often, and how much of the screen it deserves decide its weight; not the
# order the task happens to name them in.
VALUE_AND_SPACE = """\
## Value and space
Before placing anything, weigh every feature below: who needs it, how often,
and how much screen and attention it deserves. Frequent, primary actions get
the weight; rare ones stay quiet (a text link or a row, not a primary
button) or move behind a tap. Group what belongs together, so the screen
reads as a few related blocks, not a list of everything the task names.
No bare icon without a label. Beautiful and laconic is the bar."""

FREE_ZONE = """\
## Free zone
Yours to change: layout, hierarchy, grouping, spacing, style. Keep the
app's fonts, colours and components unless a change is clearly better.
Not yours: features and API (the must-keep list), and the data shown.
You may deviate from the current look if the result is better."""


@dataclass(frozen=True)
class Feature:
    """One must-keep feature of a screen (features.yaml)."""

    name: str
    action: Optional[str] = None
    api: Tuple[str, ...] = ()

    def line(self) -> str:
        parts = [self.name]
        if self.action:
            parts.append(f'action "{self.action}"')
        if self.api:
            parts.append("api " + ", ".join(self.api))
        return "- " + " · ".join(parts)


@dataclass(frozen=True)
class Brief:
    """What the generator and the critic get for one screen."""

    screen_id: str
    change_request: str
    passport: str
    source: str
    must_keep: Tuple[Feature, ...]
    passport_text: str
    tokens: str
    rules: str
    refs: Tuple[Path, ...]

    @property
    def text(self) -> str:
        must_keep = "\n".join(f.line() for f in self.must_keep) or "- (none listed)"
        refs = "\n".join(f"- {p.parent.name}/{p.name}" for p in self.refs)
        sections = [
            f"# Design brief: {self.screen_id} ({self.passport}, {self.source})",
            "## The problem\n" + self.change_request.strip(),
            "Solve this problem. The brief prescribes no variants and no "
            "layout: how to solve it is the designer's call.",
            VALUE_AND_SPACE,
            "## Must keep (docs/features.yaml)\nEvery feature stays reachable: "
            "it may move, merge or go behind a tap, never be lost.\n" + must_keep,
            "## Current screen (passport, docs/SCREENS.md)\n" + self.passport_text,
            "## Current screen (references, dark + light)\n" + (refs or "- (none)"),
            "## Shared tokens (docs/SCREENS.md §1)\n" + self.tokens,
            "## Rules (docs/UI_GUIDE.md)\n" + self.rules,
            FREE_ZONE,
        ]
        return "\n\n".join(s.strip() for s in sections if s.strip()) + "\n"


def build_brief(repo: Path, screen_id: str, change_request: str) -> Brief:
    """The brief for ``screen_id`` of the board repo ``repo``.

    ``KeyError`` for a screen features.yaml does not list, ``ValueError``
    when its passport is missing from SCREENS.md: never an empty brief.
    """
    repo = Path(repo)
    screens = load_features(repo / FEATURES_PATH)
    if screen_id not in screens:
        raise KeyError(
            f"screen {screen_id!r} is not in {FEATURES_PATH}; "
            f"one of {sorted(screens)}"
        )
    screen = screens[screen_id]
    passports = (repo / PASSPORTS_PATH).read_text(encoding="utf-8")
    passport_text = passport_section(passports, screen.passport)
    if passport_text is None:
        raise ValueError(
            f"{screen_id}: no '### {screen.passport}' passport in {PASSPORTS_PATH}"
        )
    guide = repo / GUIDE_PATH
    return Brief(
        screen_id=screen_id,
        change_request=change_request,
        passport=screen.passport,
        source=screen.source,
        must_keep=screen.features,
        passport_text=passport_text,
        tokens=tokens_section(passports),
        rules=guide.read_text(encoding="utf-8").strip() if guide.exists() else "",
        refs=screen_refs(repo, screen_id),
    )


def screen_refs(repo: Path, screen_id: str) -> Tuple[Path, ...]:
    """The current screen's reference PNGs, every state in dark and light."""
    folder = Path(repo) / REFS_DIR / screen_id
    if not folder.is_dir():
        return ()
    return tuple(
        sorted(p for p in folder.glob("*.png") if p.stem.rsplit("-", 1)[-1] in THEMES)
    )


def state_refs(repo: Path, screen_id: str, state: str) -> Tuple[Path, ...]:
    """One state's references, dark then light (only those that exist)."""
    folder = Path(repo) / REFS_DIR / screen_id
    paths = (folder / f"{state}-{theme}.png" for theme in THEMES)
    return tuple(p for p in paths if p.exists())


# --- SCREENS.md ---------------------------------------------------------------


def passport_section(text: str, passport: str) -> Optional[str]:
    """The ``###`` section whose heading, up to `` (``, is ``passport``."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("### "):
            continue
        heading = line[4:].strip()
        if heading.split(" (", 1)[0].strip() != passport.strip():
            continue
        end = i + 1
        while end < len(lines) and not re.match(r"#{2,3} ", lines[end]):
            end += 1
        return "\n".join(lines[i:end]).strip()
    return None


def tokens_section(text: str) -> str:
    """SCREENS.md §1, the shared tokens (colours, type, spacing, targets)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"## 1\.", line):
            end = i + 1
            while end < len(lines) and not lines[end].startswith("## "):
                end += 1
            return "\n".join(lines[i + 1 : end]).strip()
    return ""


# --- features.yaml ------------------------------------------------------------


@dataclass(frozen=True)
class Screen:
    passport: str
    source: str
    features: Tuple[Feature, ...]


def load_features(path: Path) -> Dict[str, Screen]:
    """``screens:`` of features.yaml: passport, source and features.

    The file is a YAML subset (gymbro's ``scripts/harness/feature_map.py``
    reads it; PyYAML chokes on its quoting), so this walks it by indent and
    keeps only the keys a brief needs: ``screens.<id>.{passport,source}``
    and ``screens.<id>.features.<name>.{action,api}``.
    """
    meta: Dict[str, Dict[str, str]] = {}
    specs: Dict[str, Dict[str, Dict[str, object]]] = {}
    stack: List[Tuple[int, str]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if stripped.startswith("- "):
            stripped = stripped[2:].lstrip()
            indent += 2
        key, sep, value = stripped.partition(":")
        if not sep:
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        keys = [k for _, k in stack] + [key.strip()]
        stack.append((indent, key.strip()))
        if not keys or keys[0] != "screens" or len(keys) < 2:
            continue
        screen = meta.setdefault(keys[1], {})
        features = specs.setdefault(keys[1], {})
        if len(keys) == 3 and keys[2] in ("passport", "source"):
            screen[keys[2]] = _scalar(value)
        elif len(keys) == 4 and keys[2] == "features":
            features.setdefault(keys[3], {})
        elif len(keys) == 5 and keys[2] == "features" and keys[4] in ("action", "api"):
            features.setdefault(keys[3], {})[keys[4]] = _value(value)
    return {
        screen_id: Screen(
            passport=data.get("passport") or screen_id,
            source=data.get("source") or "",
            features=tuple(
                Feature(
                    name=name,
                    action=_text(spec.get("action")),
                    api=_tuple(spec.get("api")),
                )
                for name, spec in specs[screen_id].items()
            ),
        )
        for screen_id, data in meta.items()
    }


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value.split(" #", 1)[0].strip()


def _value(value: str) -> object:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        items = re.findall(r'"[^"]*"|\'[^\']*\'|[^,\s][^,]*', value[1:-1])
        return [_scalar(item) for item in items if item.strip()]
    return _scalar(value)


def _text(value: object) -> Optional[str]:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or None
    return str(value) if value else None


def _tuple(value: object) -> Tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    return (str(value),) if value else ()
