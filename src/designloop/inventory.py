"""The board repo's kit components, read from its code (``design.components``).

One line per exported PascalCase component: name, file, the first sentence of
the doc comment above it. Generated at prompt time so it never goes stale; a
hand-written list did. The JS/TS ``export`` form only: another stack
gets a board argv hook when a board needs one.

Pure file reading.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Sequence, Tuple

INVENTORY_MAX = 60  # lines in a prompt

_EXPORT = re.compile(
    r"^export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|const|let|class)\s+"
    r"([A-Z][A-Za-z0-9_]*)"
)
_SENTENCE = re.compile(r"^(.+?[.!?])(?:\s|$)")


def component_files(root: Path, patterns: Sequence[str]) -> List[Path]:
    """The files ``patterns`` (paths or globs relative to ``root``) name, in
    config order, each once."""
    out: List[Path] = []
    for pattern in patterns:
        hits = sorted(root.glob(pattern)) if any(c in pattern for c in "*?[") else []
        if not hits and (root / pattern).is_file():
            hits = [root / pattern]
        for path in hits:
            if path.is_file() and path not in out:
                out.append(path)
    return out


def missing(root: Path, patterns: Sequence[str]) -> List[str]:
    """The patterns matching no file (``pump2 doctor`` reports them)."""
    return [p for p in patterns if not component_files(root, [p])]


def _doc(lines: Sequence[str], i: int) -> str:
    """The comment right above line ``i``, first sentence only."""
    j = i - 1
    while j >= 0 and not lines[j].strip():
        j -= 1
    if j < 0:
        return ""
    text = ""
    last = lines[j].strip()
    if last.endswith("*/"):
        block = [last]
        while "/*" not in block[0] and j > 0:
            j -= 1
            block.insert(0, lines[j].strip())
        if not block[0].startswith("/*"):
            return ""
        block[0] = re.sub(r"^/\*+", "", block[0])
        block[-1] = re.sub(r"\*+/$", "", block[-1])
        text = " ".join(b.lstrip("*").strip() for b in block)
    elif last.startswith("//"):
        block = []
        while j >= 0 and lines[j].strip().startswith("//"):
            block.insert(0, lines[j].strip().lstrip("/").strip())
            j -= 1
        text = " ".join(block)
    text = " ".join(text.split())
    m = _SENTENCE.match(text)
    return (m.group(1) if m else text)[:160]


def components(root: Path, patterns: Sequence[str]) -> List[Tuple[str, str, str]]:
    """(name, file relative to ``root``, doc sentence) per exported component."""
    out: List[Tuple[str, str, str]] = []
    for path in component_files(root, patterns):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(root).as_posix()
        for i, line in enumerate(lines):
            m = _EXPORT.match(line)
            if m:
                out.append((m.group(1), rel, _doc(lines, i)))
    return out


def inventory_lines(root: Path, patterns: Sequence[str]) -> List[str]:
    """The prompt's lines, capped at ``INVENTORY_MAX`` with a line saying how
    many were cut."""
    found = components(root, patterns)
    lines = [
        f"- `{name}` ({rel})" + (f": {doc}" if doc else "")
        for name, rel, doc in found[:INVENTORY_MAX]
    ]
    if len(found) > INVENTORY_MAX:
        lines.append(f"- … {len(found) - INVENTORY_MAX} more components cut")
    return lines
