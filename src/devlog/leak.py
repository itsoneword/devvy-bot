"""Leak check for channel posts (dv-fc9d): the public-release rule set, the
private terms file and a few post-specific patterns, on every draft's text
and headline, before the preview and again at publish.

One rule set: ``RULES`` / ``load_terms`` / ``term_rules`` are the ones in
``scripts/scan_public_leaks.py``, loaded from that file. They are not copied
into ``src/``: the rules spell the identities they hunt for, so a module
holding them could not ship in the public export (the scanner itself is
withheld for that reason). Without the scanner (a public install) or without
the terms file every check reports a hit: fail closed.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any, List, Optional, Sequence, Tuple, Union

REPO = Path(__file__).resolve().parents[2]
SCANNER = REPO / "scripts" / "scan_public_leaks.py"
DEFAULT_TERMS_PATH = Path.home() / "Devvybot" / "staging" / "private-terms.txt"

Rule = Tuple[str, "re.Pattern[bytes]"]

# Post-only extras: a channel post never needs a filesystem path or a raw
# key=value secret, so any is a hit.
POST_RULES: List[Rule] = [
    ("file path", re.compile(rb"(?:^|[\s(\"'`])(?:~|/home|/root|/etc|/var|/opt)/\S")),
    (
        "secret assignment",
        re.compile(rb"(?i)\b(?:api[_-]?key|token|secret|password)\s*[=:]\s*\S{8,}"),
    ),
    ("openrouter key", re.compile(rb"\bsk-or-[A-Za-z0-9_-]{10,}")),
    ("google api key", re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}")),
]


def _load_scanner() -> Optional[ModuleType]:
    if not SCANNER.is_file():
        return None
    spec = importlib.util.spec_from_file_location("scan_public_leaks", SCANNER)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_scanner = _load_scanner()
RULES: List[Rule] = list(_scanner.RULES) if _scanner is not None else []


def load_terms(path: Path) -> List[str]:
    """The private terms list; raises ``OSError`` when it cannot be read."""
    if _scanner is None:
        raise OSError(f"leak scanner missing: {SCANNER}")
    return list(_scanner.load_terms(path))


def term_rules(terms: Sequence[str]) -> List[Rule]:
    if _scanner is None:
        return []
    return list(_scanner.term_rules(terms))


def leak_hits(
    *texts: Optional[str], terms_path: Union[Path, str, None] = None
) -> List[str]:
    """Every rule that matches any of ``texts``, as ``"<label>: <snippet>"``.

    Empty = clean. A missing scanner or terms file is itself a hit."""
    path = Path(terms_path) if terms_path is not None else DEFAULT_TERMS_PATH
    hits: List[str] = []
    if _scanner is None:
        hits.append(f"leak scanner missing ({SCANNER.name}): publishing is off")
        terms: List[str] = []
    else:
        try:
            terms = load_terms(path)
        except OSError:
            hits.append(f"private terms file missing ({path}): publishing is off")
            terms = []
    rules: List[Any] = RULES + term_rules(terms) + POST_RULES
    for text in texts:
        if not text:
            continue
        for line in text.encode("utf-8").split(b"\n"):
            for label, pattern in rules:
                match = pattern.search(line)
                if match is None:
                    continue
                hit = f"{label}: {match.group(0).decode('utf-8', 'replace').strip()}"
                if hit not in hits:
                    hits.append(hit)
    return hits
