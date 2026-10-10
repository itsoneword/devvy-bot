"""Structural SQLite schema snapshot + diff (dv-5e4f).

Pure SQLite introspection, no dependency on the app's models, so it works on
all 4 DBs (bot, data, llm_analytics, channel_intel), on legacy-built copies
and on prod backups alike.

``snapshot`` is the raw truth (column info, indexes, FKs, normalized CREATE
SQL, triggers, views, FTS5 tables incl. shadow tables). It is also the format
of the committed fixtures under ``tests/fixtures/legacy_schema/``.

``diff`` compares two snapshots *structurally*: table CREATE text is NOT
compared verbatim, because a table grown by ``ALTER TABLE ADD COLUMN`` (legacy
migrations) and the same table built in one ``CREATE TABLE`` (Alembic) have
different SQL text but identical structure. Instead it compares column
position/name/declared type/notnull/default/pk, AUTOINCREMENT and CHECK
clauses (extracted from the SQL), FKs, and indexes (unique/origin/partial/
columns). Triggers, views and FTS5 virtual tables have no structure beyond
their SQL, so those are compared as whitespace-normalized SQL.

Bookkeeping tables (``schema_version``, ``alembic_version``) are ignored.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Version bookkeeping, not schema: legacy DBs have schema_version, Alembic
# DBs have alembic_version.
IGNORED_TABLES = frozenset({"schema_version", "alembic_version"})


def _normalize_sql(sql: Optional[str]) -> Optional[str]:
    """Collapse whitespace so formatting differences don't register as drift."""
    if sql is None:
        return None
    return re.sub(r"\s+", " ", sql).strip()


def _is_fts5_sql(sql: Optional[str]) -> bool:
    if not sql:
        return False
    upper = sql.upper()
    return "VIRTUAL TABLE" in upper and "FTS" in upper


def snapshot_connection(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Structural snapshot of an open sqlite3 connection's main database."""
    objects = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()

    def rows(sql: str) -> List[Dict[str, Any]]:
        cur = conn.execute(sql)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    tables: Dict[str, Any] = {}
    fts_tables: Dict[str, Any] = {}
    standalone_indexes: Dict[str, Any] = {}
    triggers: Dict[str, Any] = {}
    views: Dict[str, Any] = {}

    for kind, name, tbl_name, sql in objects:
        if kind == "table":
            index_list = rows(f'PRAGMA index_list("{name}")')
            entry = {
                "columns": rows(f'PRAGMA table_xinfo("{name}")'),
                "foreign_keys": rows(f'PRAGMA foreign_key_list("{name}")'),
                "indexes": index_list,
                "index_columns": {
                    idx["name"]: rows(f'PRAGMA index_info("{idx["name"]}")')
                    for idx in index_list
                },
                "sql": _normalize_sql(sql),
            }
            if _is_fts5_sql(sql):
                fts_tables[name] = entry
            else:
                tables[name] = entry
        elif kind == "index":
            # sqlite_autoindex_* (implicit PK/UNIQUE) are filtered by the
            # NOT LIKE above and already reflected in their table's index_list.
            standalone_indexes[name] = {
                "tbl_name": tbl_name,
                "sql": _normalize_sql(sql),
            }
        elif kind == "trigger":
            triggers[name] = {"tbl_name": tbl_name, "sql": _normalize_sql(sql)}
        elif kind == "view":
            views[name] = {"sql": _normalize_sql(sql)}

    return {
        "tables": tables,
        "fts_tables": fts_tables,
        "indexes": standalone_indexes,
        "triggers": triggers,
        "views": views,
    }


def snapshot(db_path: Union[str, Path]) -> Dict[str, Any]:
    """Snapshot ``db_path``, opened read-only (safe on a live WAL DB; for a
    consistent read of a hot DB, back it up first and snapshot the copy)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return snapshot_connection(conn)
    finally:
        conn.close()


# --- structural diff ---------------------------------------------------------


def _extract_checks(sql: str) -> List[str]:
    """Every ``CHECK (...)`` body in a CREATE TABLE, paren-balanced,
    whitespace-normalized, sorted (column-level vs table-level placement is
    not structural)."""
    out = []
    for m in re.finditer(r"\bCHECK\s*\(", sql, flags=re.IGNORECASE):
        depth, i = 1, m.end()
        while i < len(sql) and depth:
            if sql[i] == "(":
                depth += 1
            elif sql[i] == ")":
                depth -= 1
            i += 1
        body = sql[m.end() : i - 1]
        body = re.sub(r"\s+", " ", body).strip()
        body = re.sub(r"\s*([(),])\s*", r"\1", body)
        out.append(body)
    return sorted(out)


def _table_structure(entry: Dict[str, Any]) -> Dict[str, Any]:
    sql = entry["sql"] or ""
    fks: Dict[int, Dict[str, Any]] = {}
    for fk in entry["foreign_keys"]:
        g = fks.setdefault(
            fk["id"],
            {
                "table": fk["table"],
                "from": [],
                "to": [],
                "on_update": fk["on_update"],
                "on_delete": fk["on_delete"],
                "match": fk["match"],
            },
        )
        g["from"].append(fk["from"])
        g["to"].append(fk["to"])
    return {
        "columns": [
            (
                c["cid"],
                c["name"],
                c["type"],
                c["notnull"],
                c["dflt_value"],
                c["pk"],
                c["hidden"],
            )
            for c in entry["columns"]
        ],
        "autoincrement": bool(re.search(r"\bAUTOINCREMENT\b", sql, re.I)),
        "without_rowid": bool(re.search(r"\bWITHOUT\s+ROWID\b", sql, re.I)),
        "checks": _extract_checks(sql),
        # FK ids are a declaration-order artifact; compare as a sorted set.
        "foreign_keys": sorted(repr(sorted(g.items())) for g in fks.values()),
        "indexes": {
            i["name"]: (
                i["unique"],
                i["origin"],
                i["partial"],
                tuple(
                    (c["seqno"], c["name"])
                    for c in entry["index_columns"].get(i["name"], [])
                ),
            )
            for i in entry["indexes"]
        },
    }


def _where(sql: Optional[str]) -> Optional[str]:
    m = re.search(r"\bWHERE\b(.*)$", sql or "", flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def diff(
    a: Dict[str, Any], b: Dict[str, Any], a_name: str = "a", b_name: str = "b"
) -> List[str]:
    """Structural differences between two snapshots; empty list == equal."""
    out: List[str] = []

    def keyset(section: str, snap: Dict[str, Any]) -> set:
        return {k for k in snap[section] if k not in IGNORED_TABLES}

    def only(section: str) -> None:
        ka, kb = keyset(section, a), keyset(section, b)
        for k in sorted(ka - kb):
            out.append(f"{section}: {k} only in {a_name}")
        for k in sorted(kb - ka):
            out.append(f"{section}: {k} only in {b_name}")

    for section in ("tables", "fts_tables", "indexes", "triggers", "views"):
        only(section)

    for name in sorted(keyset("tables", a) & keyset("tables", b)):
        sa, sb = _table_structure(a["tables"][name]), _table_structure(
            b["tables"][name]
        )
        for field in sa:
            if sa[field] != sb[field]:
                out.append(
                    f"table {name}.{field}: {a_name}={sa[field]!r} "
                    f"{b_name}={sb[field]!r}"
                )

    for name in sorted(keyset("indexes", a) & keyset("indexes", b)):
        ia, ib = a["indexes"][name], b["indexes"][name]
        if ia["tbl_name"] != ib["tbl_name"] or _where(ia["sql"]) != _where(ib["sql"]):
            out.append(f"index {name}: {a_name}={ia!r} {b_name}={ib!r}")

    for section in ("fts_tables", "triggers", "views"):
        for name in sorted(keyset(section, a) & keyset(section, b)):
            if a[section][name]["sql"] != b[section][name]["sql"]:
                out.append(
                    f"{section} {name} sql: {a_name}={a[section][name]['sql']!r} "
                    f"{b_name}={b[section][name]['sql']!r}"
                )
    return out
