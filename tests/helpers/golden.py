"""Golden-master compare/update helper (dv-5e4f Phase 0.1, item 2).

A golden file is a normalized JSON dump of some storage-layer call's result.
Normalization: dicts are compared via Python equality (order-independent),
list-of-dict rows are sorted by a stable sort key before dumping so row
order (which SQLite does not guarantee without ORDER BY) doesn't register
as drift, and volatile fields (autoincrement ids, wall-clock timestamps) are
masked to a fixed placeholder.

Update mode: set ``DEVVY_UPDATE_GOLDEN=1`` or pass ``--update-golden`` to
pytest (registered in tests/conftest.py). In update mode, mismatches write
the new value and the test still passes (it's the mechanism for
*deliberately* refreshing a golden after an owner-reviewed behavior change,
never silent -- the test output names every file it rewrote).
"""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


def jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses/datetimes/etc. to plain JSON-able
    values, so golden dumps are comparable across runs and readable in a
    diff (no repr() noise, no object identity)."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj


GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "golden"

# Field names masked to a fixed placeholder wherever they appear in a dict,
# at any nesting depth. These are either autoincrement surrogate keys or
# wall-clock-dependent, so a passing re-run can legitimately differ in value
# while being behaviorally identical.
_MASKED_TIMESTAMP_KEYS = {
    "created_at",
    "updated_at",
    "timestamp",
    "ts",
    "first_seen",
    "last_active",
    "last_used",
    "received_at",
    "set_at",
    "recorded_at",
    "valid_from",
    "done_at",
    "last_scan_at",
    "enriched_at",
    "held_until",
    "last_activity",
    "last_fired_at",
    "deactivated_at",
    "updated_on",
}
_MASKED_ID_KEYS = {
    "message_id",
    "id",
}
# Server-generated opaque string ids (uuid4 hex etc.) -- masked regardless
# of type, unlike the deterministic ids this suite assigns itself
# (session_id "sess-1", request_id "req_s1_1", ...).
_MASKED_OPAQUE_STRING_KEYS = {
    "job_id",
}


def _is_update_mode() -> bool:
    return os.environ.get("DEVVY_UPDATE_GOLDEN") == "1"


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def mask(value: Any, extra_id_keys: frozenset = frozenset()) -> Any:
    """Recursively replace volatile fields with stable placeholders."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _MASKED_TIMESTAMP_KEYS and isinstance(v, (str, type(None))):
                out[k] = "<ts>" if v is not None else None
            elif k in (_MASKED_ID_KEYS | extra_id_keys) and isinstance(v, int):
                out[k] = "<id>"
            elif k in _MASKED_OPAQUE_STRING_KEYS and isinstance(v, str):
                out[k] = "<opaque_id>"
            elif k == "date" and v == _today_utc():
                # SQLite date('now') defaults; fixed seed dates stay visible.
                out[k] = "<today>"
            else:
                out[k] = mask(v, extra_id_keys)
        return out
    if isinstance(value, list):
        return [mask(v, extra_id_keys) for v in value]
    return value


def sort_rows(rows: list, key: str = "id") -> list:
    """Sort a list of dict rows by ``key`` (falls back to a stable repr sort
    if the key is absent/masked) so row order never registers as drift."""
    try:
        return sorted(rows, key=lambda r: (r.get(key) is None, r.get(key)))
    except TypeError:
        return sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=str))


def compare_or_update(name: str, data: Any, redact: dict | None = None) -> list:
    """Compare ``data`` (already normalized/masked) against the committed
    golden file ``tests/fixtures/golden/<name>.json``.

    ``redact`` is an optional {literal_substring: placeholder} map applied
    to the dumped text before writing/comparing -- for run-specific values
    that don't fit the dict-key-based ``mask()`` scheme, chiefly a pytest
    ``tmp_path`` that shows up embedded inside file paths.

    Returns a list of human-readable mismatch descriptions (empty = match).
    In update mode, a mismatch (or missing file) is written and reported as
    an informational "updated" entry rather than a failure -- the caller
    decides whether an "updated" entry should still fail the test (it
    should NOT fail when --update-golden was explicitly requested).
    """
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = GOLDEN_DIR / f"{name}.json"
    text = json.dumps(data, indent=2, sort_keys=True, default=str) + "\n"
    for literal, placeholder in (redact or {}).items():
        text = text.replace(literal, placeholder)

    if not path.exists():
        path.write_text(text)
        return [f"CREATED {path} (no prior golden -- review and commit)"]

    existing = path.read_text()
    if existing == text:
        return []

    if _is_update_mode():
        path.write_text(text)
        return [f"UPDATED {path} (--update-golden/DEVVY_UPDATE_GOLDEN=1)"]

    return [f"MISMATCH {path} -- rerun with DEVVY_UPDATE_GOLDEN=1 to inspect/update"]


def is_update_mode() -> bool:
    return _is_update_mode()


def has_failures(messages: list) -> bool:
    """True if ``messages`` (from compare_or_update) contains a real
    mismatch. CREATED/UPDATED entries are informational, not failures."""
    return any(m.startswith("MISMATCH") for m in messages)
