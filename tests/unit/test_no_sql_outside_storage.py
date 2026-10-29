"""dv-5e4f Phase 0 guard: raw SQL stays in the storage layer.

Every direct ``get_connection(`` / ``.execute(`` / ``.executescript(`` call
against a DB connection now lives in ``src/storage/``, ``src/items/
repository.py``, ``src/channel_intel/`` or ``src/llm_analytics/`` (the last
two are documented standalone DBs / cross-process read-only access -- see
their READMEs). A new raw-SQL leak outside those directories is a regression
of the Phase 0 cleanup and fails here (pattern of
tests/unit/test_no_stray_model_calls.py).
"""

import re
from pathlib import Path
from typing import Set

REPO = Path(__file__).resolve().parents[2]

ALLOWED_PREFIXES = (
    "src/storage/",
    "src/items/repository.py",
    "src/channel_intel/",
    "src/llm_analytics/",
    # Standalone writer of the separate Postgres observability DB (psycopg,
    # obs_writer role); not the bot's SQLite, same class as llm_analytics.
    "src/observability/host_metrics.py",
)

_PATTERN = re.compile(r"\bget_connection\(|\.execute\(|\.executescript\(")

_SKIP_DIRS = {"__pycache__", "node_modules", "reports", ".venv"}


def _sources():
    for path in (REPO / "src").rglob("*.py"):
        if _SKIP_DIRS & set(path.parts):
            continue
        yield path


def test_no_raw_sql_outside_storage_layer():
    stray: Set[str] = set()
    for path in _sources():
        rel = str(path.relative_to(REPO))
        if any(rel == p or rel.startswith(p) for p in ALLOWED_PREFIXES):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if _PATTERN.search(text):
            for lineno, line in enumerate(text.splitlines(), 1):
                if _PATTERN.search(line):
                    stray.add(f"{rel}:{lineno}: {line.strip()}")
    assert stray == set()


def test_allowed_prefixes_still_exist():
    for prefix in ALLOWED_PREFIXES:
        assert (REPO / prefix).exists(), prefix
