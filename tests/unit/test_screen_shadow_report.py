"""scripts/screen_shadow_report.py: old vs shadow ops over a date range."""

import importlib.util
import io
import json
import sqlite3
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "screen_shadow_report.py"
spec = importlib.util.spec_from_file_location("screen_shadow_report", SCRIPT)
report_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report_mod)


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE memory_extractions (id INTEGER PRIMARY KEY, timestamp TEXT, "
        "validation_status TEXT, parsed_insights TEXT, target_files TEXT, op_stats TEXT)"
    )
    db.execute("CREATE TABLE model_calls (ts TEXT, purpose TEXT, cost_usd REAL)")
    return db


def _row(db, rid, ts, old_ops, screen, verdicts=()):
    stats = {"verdicts": list(verdicts), "screen": screen}
    db.execute(
        "INSERT INTO memory_extractions VALUES (?,?,?,?,?,?)",
        (
            rid,
            ts,
            "success",
            json.dumps(old_ops),
            json.dumps(["devops"]),
            json.dumps(stats),
        ),
    )


def _chunk(sent, ops, total=10, capped=False):
    return {
        "status": "ok",
        "facts": {
            "sent": len(sent),
            "total": total,
            "sent_chars": len(sent) * 100,
            "total_chars": total * 100,
            "capped": capped,
        },
        "selected": [{"id": i, "t": 0.9, "m": 0.9, "why": "score"} for i in sent],
        "gate": {"remember_request": 0.9, "change_or_end": 0.2},
        "ops": ops,
    }


def test_report_recall_and_op_agreement():
    db = _db()
    _row(
        db,
        1,
        "2026-10-18 10:00:00",
        [
            {
                "op": "supersede",
                "content": "bench press max is 110 kg",
                "supersedes_id": 5,
            },
            {"op": "add", "content": "only the classic path saw this"},
        ],
        {
            "status": "ok",
            "chunks": [
                _chunk(
                    [5, 6],
                    [
                        {
                            "op": "supersede",
                            "content": "Bench press max: 110kg",
                            "supersedes_id": 5,
                        },
                        {"op": "add", "content": "something else entirely new"},
                    ],
                ),
                {"status": "jev_failed", "error": "x"},
            ],
        },
        verdicts=[{"verdict": "duplicate", "existing_id": 9}],
    )
    _row(db, 2, "2026-09-01 10:00:00", [], {"status": "ok", "chunks": []})
    db.execute("INSERT INTO model_calls VALUES ('2026-10-18 10:00:00','screen',0.002)")
    db.execute(
        "INSERT INTO model_calls VALUES ('2026-10-18 10:00:00','extraction',0.4)"
    )

    rows = report_mod.load_rows(db, "2026-10-01", "2026-10-31", None)
    assert [r["id"] for r in rows] == [1]  # date range filter
    assert report_mod.classic_refs(rows[0]) == {5, 9}
    assert report_mod.sent_ids(rows[0]) == {5, 6}

    out = io.StringIO()
    report_mod.report(
        rows, report_mod.call_costs(db, "2026-10-01", "2026-10-31"), True, out
    )
    text = out.getvalue()
    assert "in the sent set: 1 / 2 (50%)" in text  # 9 was never sent
    assert "missed (row, fact id): 1:9" in text
    assert (
        "matched 1 (same supersede target 1, different 0); only classic 1; only screen 1"
        in text
    )
    assert "{'jev_failed': 1, 'ok': 1}" in text
    assert "extraction" in text and "$0.4000" in text
    assert "only classic: add 'only the classic path saw this'" in text


def test_empty_range():
    out = io.StringIO()
    report_mod.report([], {}, False, out)
    assert "no rows" in out.getvalue()


def test_agent_filter():
    db = _db()
    _row(db, 1, "2026-10-18 10:00:00", [], {"status": "ok", "chunks": []})
    assert report_mod.load_rows(db, "2026-10-01", "2026-10-31", "chef") == []
