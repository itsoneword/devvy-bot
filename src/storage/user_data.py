"""Per-user data store (dv-5f3a): data.db + the single enforced access layer.

Separate database file from bot.db by owner decision: user-related data
(logs, products, portions, issues, docs) lives here; bot.db stays
analytic/monitoring. ``UserDataManager`` owns the file and its own migration
chain; ``UserDataRepository`` is THE ONLY code that executes SQL against
data.db — every per-user method takes ``user_id`` as its first argument and
no method returns rows across users. This is the one place a missing
predicate can hide, and tests cover exactly that.

Rules enforced here (owner decisions 2026-08-15):
- Parameterized SQL only. All statements are static string literals with
  ``?`` placeholders; ``tests/unit/test_storage/test_user_data.py`` walks the
  AST of this module and fails on any f-string/concat/format SQL.
- Write methods return computed aggregates (day totals, merged sleep row,
  previous-session sets) — arithmetic lives here, testable, never in the
  model.
- Provenance guard lives here, not in tools: an ``estimate`` product never
  overwrites a ``label_photo``/``official`` row.
- ``products`` is shared across users by design (data scope, not access
  control — nutrition data is universal); everything else is per-user.
- Empty values stay NULL, never 0 (dv-3668: "Пустая ячейка остаётся пустой").
"""

from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite
import structlog

from .database import DatabaseManager

logger = structlog.get_logger()


def _casefold(value: Optional[str]) -> Optional[str]:
    """Unicode-aware case folding, exposed to SQL as ``py_casefold``.

    SQLite's ``lower()``/``upper()`` and ``COLLATE NOCASE`` fold ASCII only,
    and this build has no ICU: to plain SQL, "Зефир" and "зефир" are two
    unrelated strings. Nearly all of this data is Russian, so that is not an
    edge case, it is the normal one — see the callers below.
    """
    return value.casefold() if isinstance(value, str) else None


def _now() -> str:
    return datetime.now(UTC).isoformat()


# Correcting and removing rows (2026-08-17). Editing uses COALESCE, which by
# construction cannot write NULL — so clearing one field gets its own literal
# statement per column, keeping the single-seam rule (no SQL built at runtime)
# while still letting the agent take back a value it invented.
CLEARABLE_SQL: Dict[str, Dict[str, str]] = {
    "food": {
        "time": "UPDATE logs_food SET time = NULL WHERE id = ? AND user_id = ?",
        "kcal": "UPDATE logs_food SET kcal = NULL WHERE id = ? AND user_id = ?",
        "protein": "UPDATE logs_food SET protein = NULL WHERE id = ? AND user_id = ?",
        "fat": "UPDATE logs_food SET fat = NULL WHERE id = ? AND user_id = ?",
        "carb": "UPDATE logs_food SET carb = NULL WHERE id = ? AND user_id = ?",
        "confidence": (
            "UPDATE logs_food SET confidence = NULL WHERE id = ? AND user_id = ?"
        ),
        "source": "UPDATE logs_food SET source = NULL WHERE id = ? AND user_id = ?",
    },
    "train": {
        "session": "UPDATE logs_train SET session = NULL WHERE id = ? AND user_id = ?",
        "set_n": "UPDATE logs_train SET set_n = NULL WHERE id = ? AND user_id = ?",
        "weight_kg": (
            "UPDATE logs_train SET weight_kg = NULL WHERE id = ? AND user_id = ?"
        ),
        "reps": "UPDATE logs_train SET reps = NULL WHERE id = ? AND user_id = ?",
        "rpe": "UPDATE logs_train SET rpe = NULL WHERE id = ? AND user_id = ?",
    },
    "activity": {
        "duration_min": (
            "UPDATE logs_activity SET duration_min = NULL "
            "WHERE id = ? AND user_id = ?"
        ),
        "intensity": (
            "UPDATE logs_activity SET intensity = NULL WHERE id = ? AND user_id = ?"
        ),
        "kcal_active": (
            "UPDATE logs_activity SET kcal_active = NULL "
            "WHERE id = ? AND user_id = ?"
        ),
        "kcal_total": (
            "UPDATE logs_activity SET kcal_total = NULL WHERE id = ? AND user_id = ?"
        ),
        "exercise_min": (
            "UPDATE logs_activity SET exercise_min = NULL "
            "WHERE id = ? AND user_id = ?"
        ),
        "stand_h": (
            "UPDATE logs_activity SET stand_h = NULL WHERE id = ? AND user_id = ?"
        ),
        "steps": ("UPDATE logs_activity SET steps = NULL WHERE id = ? AND user_id = ?"),
        "distance_km": (
            "UPDATE logs_activity SET distance_km = NULL "
            "WHERE id = ? AND user_id = ?"
        ),
    },
}

# Sleep is the one domain keyed on (user_id, date) rather than a row id
# (dv-4e81), so its clear statements bind the date, not an entry_id, and it
# gets its own map instead of a fourth entry in CLEARABLE_SQL — one dict whose
# values take different parameters is exactly the kind of thing that gets
# called with the wrong tuple. `imported_from` is deliberately absent: it
# records where a row came from, and an import's provenance is not the agent's
# to erase.
CLEARABLE_SLEEP_SQL: Dict[str, str] = {
    "bed": "UPDATE logs_sleep SET bed = NULL WHERE user_id = ? AND date = ?",
    "wake": "UPDATE logs_sleep SET wake = NULL WHERE user_id = ? AND date = ?",
    "sleep_min": (
        "UPDATE logs_sleep SET sleep_min = NULL WHERE user_id = ? AND date = ?"
    ),
    "onset_min": (
        "UPDATE logs_sleep SET onset_min = NULL WHERE user_id = ? AND date = ?"
    ),
    "score": "UPDATE logs_sleep SET score = NULL WHERE user_id = ? AND date = ?",
    "hrv": "UPDATE logs_sleep SET hrv = NULL WHERE user_id = ? AND date = ?",
    "rhr": "UPDATE logs_sleep SET rhr = NULL WHERE user_id = ? AND date = ?",
    "weight_kg": (
        "UPDATE logs_sleep SET weight_kg = NULL WHERE user_id = ? AND date = ?"
    ),
    "wellbeing": (
        "UPDATE logs_sleep SET wellbeing = NULL WHERE user_id = ? AND date = ?"
    ),
    "alcohol_u": (
        "UPDATE logs_sleep SET alcohol_u = NULL WHERE user_id = ? AND date = ?"
    ),
    "thc": "UPDATE logs_sleep SET thc = NULL WHERE user_id = ? AND date = ?",
    "room_temp": (
        "UPDATE logs_sleep SET room_temp = NULL WHERE user_id = ? AND date = ?"
    ),
    "out_night": (
        "UPDATE logs_sleep SET out_night = NULL WHERE user_id = ? AND date = ?"
    ),
    "out_day": "UPDATE logs_sleep SET out_day = NULL WHERE user_id = ? AND date = ?",
}

# Deleting a row: one literal statement per domain, every one user-scoped so
# an id belonging to somebody else matches nothing.
DELETE_SQL: Dict[str, str] = {
    "food": "DELETE FROM logs_food WHERE id = ? AND user_id = ?",
    "sleep": "DELETE FROM logs_sleep WHERE id = ? AND user_id = ?",
    "train": "DELETE FROM logs_train WHERE id = ? AND user_id = ?",
    "activity": "DELETE FROM logs_activity WHERE id = ? AND user_id = ?",
}

SELECT_SQL: Dict[str, str] = {
    "food": "SELECT * FROM logs_food WHERE id = ? AND user_id = ?",
    "sleep": "SELECT * FROM logs_sleep WHERE id = ? AND user_id = ?",
    "train": "SELECT * FROM logs_train WHERE id = ? AND user_id = ?",
    "activity": "SELECT * FROM logs_activity WHERE id = ? AND user_id = ?",
}


# Batch writes (dv-b042). Each INSERT is hoisted to a module constant so the
# one-row method and the many-row method execute the SAME literal — a batch
# path with its own copy of the SQL is a place for the two to drift, and the
# drift would only show up as wrong data. Batch methods loop these inside ONE
# connection and commit ONCE: the pool hands out connections at the default
# deferred isolation level, so the loop is a single transaction and a failure
# part-way leaves nothing behind.
INSERT_FOOD_SQL = (
    "INSERT INTO logs_food (user_id, date, meal, time, kcal, "
    "protein, fat, carb, confidence, source, imported_from, "
    "created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

INSERT_TRAIN_SQL = (
    "INSERT INTO logs_train (user_id, date, session, exercise, "
    "set_n, weight_kg, reps, rpe, imported_from, created_at, "
    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

INSERT_ACTIVITY_SQL = (
    "INSERT INTO logs_activity (user_id, date, type, duration_min, "
    "intensity, kcal_active, kcal_total, exercise_min, stand_h, "
    "steps, distance_km, source, imported_from, created_at, "
    "updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

UPSERT_SLEEP_SQL = (
    "INSERT INTO logs_sleep (user_id, date, bed, wake, sleep_min, "
    "onset_min, score, hrv, rhr, weight_kg, wellbeing, alcohol_u, "
    "thc, room_temp, out_night, out_day, imported_from, "
    "created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(user_id, date) DO UPDATE SET "
    "bed = COALESCE(excluded.bed, logs_sleep.bed), "
    "wake = COALESCE(excluded.wake, logs_sleep.wake), "
    "sleep_min = COALESCE(excluded.sleep_min, logs_sleep.sleep_min), "
    "onset_min = COALESCE(excluded.onset_min, logs_sleep.onset_min), "
    "score = COALESCE(excluded.score, logs_sleep.score), "
    "hrv = COALESCE(excluded.hrv, logs_sleep.hrv), "
    "rhr = COALESCE(excluded.rhr, logs_sleep.rhr), "
    "weight_kg = COALESCE(excluded.weight_kg, logs_sleep.weight_kg), "
    "wellbeing = COALESCE(excluded.wellbeing, logs_sleep.wellbeing), "
    "alcohol_u = COALESCE(excluded.alcohol_u, logs_sleep.alcohol_u), "
    "thc = COALESCE(excluded.thc, logs_sleep.thc), "
    "room_temp = COALESCE(excluded.room_temp, logs_sleep.room_temp), "
    "out_night = COALESCE(excluded.out_night, logs_sleep.out_night), "
    "out_day = COALESCE(excluded.out_day, logs_sleep.out_day), "
    "imported_from = COALESCE(excluded.imported_from, "
    "logs_sleep.imported_from), "
    "updated_at = excluded.updated_at"
)

# dv-4e81. Same COALESCE-per-column shape as the upsert, minus the INSERT:
# `sleep_edit` corrects a night that already exists and must NOT create one.
# Writing a night is `log_sleep`; a typo'd date here has to fail, not silently
# open a new row that then looks like real data.
UPDATE_SLEEP_SQL = (
    "UPDATE logs_sleep SET "
    "bed = COALESCE(?, bed), "
    "wake = COALESCE(?, wake), "
    "sleep_min = COALESCE(?, sleep_min), "
    "onset_min = COALESCE(?, onset_min), "
    "score = COALESCE(?, score), "
    "hrv = COALESCE(?, hrv), "
    "rhr = COALESCE(?, rhr), "
    "weight_kg = COALESCE(?, weight_kg), "
    "wellbeing = COALESCE(?, wellbeing), "
    "alcohol_u = COALESCE(?, alcohol_u), "
    "thc = COALESCE(?, thc), "
    "room_temp = COALESCE(?, room_temp), "
    "out_night = COALESCE(?, out_night), "
    "out_day = COALESCE(?, out_day), "
    "updated_at = ? "
    "WHERE user_id = ? AND date = ?"
)

SELECT_SLEEP_DAY_SQL = "SELECT * FROM logs_sleep WHERE user_id = ? AND date = ?"

SELECT_SLEEP_AVG_SQL = (
    "SELECT AVG(sleep_min) AS sleep_min, AVG(score) AS score, "
    "AVG(hrv) AS hrv, AVG(rhr) AS rhr, AVG(weight_kg) AS weight_kg "
    "FROM logs_sleep "
    "WHERE user_id = ? AND date >= date(?, '-6 days') AND date <= ?"
)

SELECT_TRAIN_DAY_EXERCISE_SQL = (
    "SELECT * FROM logs_train WHERE user_id = ? AND date = ? "
    "AND exercise = ? ORDER BY id"
)

SELECT_TRAIN_PREV_DATE_SQL = (
    "SELECT MAX(date) AS date FROM logs_train "
    "WHERE user_id = ? AND exercise = ? AND date < ?"
)

SELECT_ACTIVITY_DAY_SQL = (
    "SELECT * FROM logs_activity WHERE user_id = ? AND date = ? ORDER BY id"
)


def _params_food(
    user_id: int, date: str, row: Dict[str, Any], now: str
) -> Tuple[Any, ...]:
    """Bind one meal row. ``.get`` returns None for anything absent, and None
    is stored as NULL — never 0 (dv-3668), which is what keeps a missing
    number out of the day's averages instead of dragging them down."""
    return (
        user_id,
        date,
        row.get("meal"),
        row.get("time"),
        row.get("kcal"),
        row.get("protein"),
        row.get("fat"),
        row.get("carb"),
        row.get("confidence"),
        row.get("source"),
        row.get("imported_from"),
        now,
        now,
    )


def _params_train(
    user_id: int, date: str, session: Optional[str], row: Dict[str, Any], now: str
) -> Tuple[Any, ...]:
    """Bind one set row. ``session`` is batch-level (a workout has one) but a
    row may override it; weight_kg/reps stay TEXT so '10-12' survives."""
    return (
        user_id,
        date,
        row.get("session") if row.get("session") is not None else session,
        row.get("exercise"),
        row.get("set_n"),
        row.get("weight_kg"),
        row.get("reps"),
        row.get("rpe"),
        row.get("imported_from"),
        now,
        now,
    )


def _params_activity(
    user_id: int, date: str, row: Dict[str, Any], now: str
) -> Tuple[Any, ...]:
    """Bind one activity row. ``type``/``source`` are validated by the caller
    before any statement runs — the batch is rejected whole, not part-way."""
    return (
        user_id,
        date,
        row.get("type"),
        row.get("duration_min"),
        row.get("intensity"),
        row.get("kcal_active"),
        row.get("kcal_total"),
        row.get("exercise_min"),
        row.get("stand_h"),
        row.get("steps"),
        row.get("distance_km"),
        row.get("source"),
        row.get("imported_from"),
        now,
        now,
    )


def _params_sleep(user_id: int, row: Dict[str, Any], now: str) -> Tuple[Any, ...]:
    """Bind one night. ``date`` is per-row here, not batch-level: a batch of
    nights is normally several dates at once (a week off a watch export)."""
    return (
        user_id,
        row.get("date"),
        row.get("bed"),
        row.get("wake"),
        row.get("sleep_min"),
        row.get("onset_min"),
        row.get("score"),
        row.get("hrv"),
        row.get("rhr"),
        row.get("weight_kg"),
        row.get("wellbeing"),
        row.get("alcohol_u"),
        row.get("thc"),
        row.get("room_temp"),
        row.get("out_night"),
        row.get("out_day"),
        row.get("imported_from"),
        now,
        now,
    )


def _unique(values: List[Any]) -> List[Any]:
    """Order-preserving dedup. The batch payload is built per DISTINCT key,
    so an eleven-set batch across four exercises returns four previous-session
    blocks, not eleven (dv-b042 acceptance)."""
    return list(dict.fromkeys(values))


class UserDataManager(DatabaseManager):
    """data.db connection owner: own file, own Alembic history."""

    schema_name = "data"

    async def _new_connection(self) -> aiosqlite.Connection:
        """Every data.db connection carries ``py_casefold``.

        Registered per connection (there is no other hook — the pool hands
        out long-lived connections), so name lookups can fold case the way
        the language of this data actually works. Not usable in an index:
        SQLite forbids application-defined functions there. The tables are
        small (tens of products), the scans are already full scans because
        every pattern starts with ``%``, so nothing is lost.
        """
        conn = await super()._new_connection()
        await conn.create_function("py_casefold", 1, _casefold, deterministic=True)
        return conn


class UserDataRepository:
    """The single choke point for data.db SQL.

    Every per-user method takes ``user_id`` first and scopes every statement
    with it. ``products`` reads take no user_id (shared data); product writes
    carry ``created_by`` for audit only.
    """

    def __init__(self, db: UserDataManager) -> None:
        self._db = db

    # -- food ---------------------------------------------------------------

    async def log_food(
        self,
        user_id: int,
        date: str,
        meal: str,
        time: Optional[str] = None,
        kcal: Optional[float] = None,
        protein: Optional[float] = None,
        fat: Optional[float] = None,
        carb: Optional[float] = None,
        confidence: Optional[str] = None,
        source: Optional[str] = None,
        imported_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert one meal row; returns the row plus computed day totals."""
        result = await self.log_food_items(
            user_id,
            date,
            [
                {
                    "meal": meal,
                    "time": time,
                    "kcal": kcal,
                    "protein": protein,
                    "fat": fat,
                    "carb": carb,
                    "confidence": confidence,
                    "source": source,
                    "imported_from": imported_from,
                }
            ],
        )
        return {
            "id": result["ids"][0],
            "date": date,
            "meal": meal,
            "day": result["day"],
        }

    async def log_food_items(
        self, user_id: int, date: str, items: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Insert many meal rows for one date in a single transaction.

        dv-b042: one multi-component meal was one tool call per component —
        14 rows in a minute on 2026-08-19 — and every call re-returned the
        day's totals, so the same block landed in context fourteen times.
        Here the inserts share one commit and the totals are computed once,
        AFTER all rows are in, so the number the model quotes is the final
        one rather than a running subtotal it has to reconcile.
        """
        now = _now()
        async with self._db.get_connection() as conn:
            ids: List[int] = []
            for row in items:
                cur = await conn.execute(
                    INSERT_FOOD_SQL, _params_food(user_id, date, row, now)
                )
                ids.append(int(cur.lastrowid or 0))
            await conn.commit()
        return {
            "written": len(ids),
            "ids": ids,
            "date": date,
            "meals": [row.get("meal") for row in items],
            "day": await self.day_food_totals(user_id, date),
        }

    async def edit_food(
        self,
        user_id: int,
        entry_id: int,
        meal: Optional[str] = None,
        time: Optional[str] = None,
        kcal: Optional[float] = None,
        protein: Optional[float] = None,
        fat: Optional[float] = None,
        carb: Optional[float] = None,
        confidence: Optional[str] = None,
        source: Optional[str] = None,
        clear: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Correct an existing meal row in place, provided fields only.

        Without this the agent could only append a "КОРРЕКЦИЯ … -300 kcal"
        row: day totals stayed right, but the log stopped being a record of
        meals (dv-df45 follow-up, 2026-08-17).

        ``COALESCE(?, col)`` gives partial update with static SQL — the
        single-seam rule forbids building a column list at runtime. Since
        COALESCE can never write NULL, ``clear`` names the fields to blank
        out instead (each its own literal statement), so a number the agent
        invented can be taken back rather than left standing.

        ``WHERE id = ? AND user_id = ?`` is the isolation guarantee: an id
        belonging to another user matches nothing, so a wrong or invented id
        is a no-op, never a cross-user edit.
        """
        now = _now()
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM logs_food WHERE id = ? AND user_id = ?",
                (entry_id, user_id),
            )
            before = await cur.fetchone()
            if before is None:
                return {"edited": False, "reason": "no such entry for this user"}
            await conn.execute(
                "UPDATE logs_food SET "
                "meal = COALESCE(?, meal), "
                "time = COALESCE(?, time), "
                "kcal = COALESCE(?, kcal), "
                "protein = COALESCE(?, protein), "
                "fat = COALESCE(?, fat), "
                "carb = COALESCE(?, carb), "
                "confidence = COALESCE(?, confidence), "
                "source = COALESCE(?, source), "
                "updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (
                    meal,
                    time,
                    kcal,
                    protein,
                    fat,
                    carb,
                    confidence,
                    source,
                    now,
                    entry_id,
                    user_id,
                ),
            )
            cleared = await self._clear_fields(conn, "food", entry_id, user_id, clear)
            await conn.commit()
            cur = await conn.execute(
                "SELECT * FROM logs_food WHERE id = ? AND user_id = ?",
                (entry_id, user_id),
            )
            after = await cur.fetchone()
        date = str(dict(before)["date"])
        logger.info(
            "Food entry edited",
            user_id=user_id,
            entry_id=entry_id,
            before=dict(before),
            after=dict(after) if after else None,
            cleared=cleared,
        )
        return {
            "edited": True,
            "row": dict(after) if after else None,
            "day": await self.day_food_totals(user_id, date),
        }

    async def _clear_fields(
        self, conn: Any, domain: str, entry_id: int, user_id: int, fields: Any
    ) -> List[str]:
        """Set the named columns back to NULL. Unknown names are ignored so a
        model typo can never widen into a wrong statement."""
        cleared: List[str] = []
        for name in fields or []:
            sql = CLEARABLE_SQL.get(domain, {}).get(str(name).strip().lower())
            if sql:
                await conn.execute(sql, (entry_id, user_id))
                cleared.append(str(name).strip().lower())
        return cleared

    async def delete_log_entry(
        self, user_id: int, domain: str, entry_id: int
    ) -> Dict[str, Any]:
        """Remove one log row outright.

        Hard delete rather than a soft-delete flag: a tombstone column would
        have to be honoured by every read in this class, and a missed one
        resurrects the row in a total. The row is written to the journal
        before it goes, which is where a recovery would come from.
        """
        select = SELECT_SQL.get(domain)
        delete = DELETE_SQL.get(domain)
        if not select or not delete:
            return {"deleted": False, "reason": f"unknown domain {domain!r}"}
        async with self._db.get_connection() as conn:
            cur = await conn.execute(select, (entry_id, user_id))
            row = await cur.fetchone()
            if row is None:
                return {"deleted": False, "reason": "no such entry for this user"}
            await conn.execute(delete, (entry_id, user_id))
            await conn.commit()
        removed = dict(row)
        logger.info(
            "Log entry deleted",
            user_id=user_id,
            domain=domain,
            entry_id=entry_id,
            row=removed,
        )
        result: Dict[str, Any] = {"deleted": True, "row": removed}
        date = removed.get("date")
        if domain == "food" and date:
            result["day"] = await self.day_food_totals(user_id, str(date))
        return result

    async def missing_entry_index(
        self, user_id: int, domain: str, entry_ids: List[int]
    ) -> Optional[int]:
        """Index of the first id in the batch that is not one of this user's
        rows, or None when every id checks out.

        The pre-flight for every batch edit/delete (dv-b042). A batch whose
        third id is wrong must not leave the first two applied and then fail:
        the model would have no way to tell which half landed, and the honest
        recovery — re-reading day_summary — is exactly what it would skip.
        Checking first turns that into a clean "nothing happened, row N is
        bad". ``WHERE id = ? AND user_id = ?`` means another user's id is
        simply missing, never visible.
        """
        select = SELECT_SQL.get(domain)
        if not select:
            return 0
        async with self._db.get_connection() as conn:
            for index, entry_id in enumerate(entry_ids):
                cur = await conn.execute(select, (entry_id, user_id))
                if await cur.fetchone() is None:
                    return index
        return None

    async def edit_food_many(
        self, user_id: int, edits: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Apply several meal corrections, ids pre-flighted as a set.

        Each row's UPDATE is individually atomic (``edit_food``); what the
        batch adds is that a bad id aborts before anything is written. Day
        totals are recomputed once per distinct date at the end, so the model
        quotes a settled number rather than one subtotal per correction.
        """
        missing = await self.missing_entry_index(
            user_id, "food", [int(e.get("entry_id") or 0) for e in edits]
        )
        if missing is not None:
            return {"edited": 0, "bad_index": missing}
        rows: List[Optional[Dict[str, Any]]] = []
        for edit in edits:
            result = await self.edit_food(user_id, **edit)
            rows.append(result.get("row"))
        days: Dict[str, Any] = {}
        for date in _unique([r.get("date") for r in rows if r]):
            days[str(date)] = await self.day_food_totals(user_id, str(date))
        return {"edited": len(rows), "rows": rows, "days": days}

    async def edit_train_many(
        self, user_id: int, edits: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Apply several set corrections, ids pre-flighted as a set (dv-b042).

        The common case is one exercise re-weighted across all its sets
        («все подходы были с 45, не 40»), which was one call per set.
        """
        missing = await self.missing_entry_index(
            user_id, "train", [int(e.get("entry_id") or 0) for e in edits]
        )
        if missing is not None:
            return {"edited": 0, "bad_index": missing}
        rows = [(await self.edit_train(user_id, **edit)).get("row") for edit in edits]
        return {"edited": len(rows), "rows": rows}

    async def edit_activity_many(
        self, user_id: int, edits: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Apply several activity corrections, ids pre-flighted as a set."""
        missing = await self.missing_entry_index(
            user_id, "activity", [int(e.get("entry_id") or 0) for e in edits]
        )
        if missing is not None:
            return {"edited": 0, "bad_index": missing}
        rows = [
            (await self.edit_activity(user_id, **edit)).get("row") for edit in edits
        ]
        return {"edited": len(rows), "rows": rows}

    async def delete_log_entries(
        self, user_id: int, entries: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Remove several log rows, all ids pre-flighted before any deletion.

        dv-b042: undoing a mis-logged session was one call per set, and a
        half-finished delete is the worst possible state — the user is told
        the session is gone while some of it still counts toward the day.
        Every id is checked first, per its own domain; one bad id and nothing
        is removed. Each removed row is journalled by ``delete_log_entry``.
        """
        for index, entry in enumerate(entries):
            domain = str(entry.get("domain") or "")
            missing = await self.missing_entry_index(
                user_id, domain, [int(entry.get("entry_id") or 0)]
            )
            if missing is not None:
                return {"deleted": 0, "bad_index": index}
        removed: List[Optional[Dict[str, Any]]] = []
        for entry in entries:
            result = await self.delete_log_entry(
                user_id, str(entry["domain"]), int(entry["entry_id"])
            )
            removed.append(result.get("row"))
        days: Dict[str, Any] = {}
        for date in _unique(
            [
                r.get("date")
                for r, e in zip(removed, entries)
                if r and e.get("domain") == "food"
            ]
        ):
            days[str(date)] = await self.day_food_totals(user_id, str(date))
        return {"deleted": len(removed), "rows": removed, "days": days}

    async def edit_train(
        self,
        user_id: int,
        entry_id: int,
        exercise: Optional[str] = None,
        session: Optional[str] = None,
        set_n: Optional[str] = None,
        weight_kg: Optional[str] = None,
        reps: Optional[str] = None,
        rpe: Optional[float] = None,
        clear: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Correct one training set in place. Same contract as edit_food."""
        now = _now()
        async with self._db.get_connection() as conn:
            cur = await conn.execute(SELECT_SQL["train"], (entry_id, user_id))
            before = await cur.fetchone()
            if before is None:
                return {"edited": False, "reason": "no such entry for this user"}
            await conn.execute(
                "UPDATE logs_train SET "
                "exercise = COALESCE(?, exercise), "
                "session = COALESCE(?, session), "
                "set_n = COALESCE(?, set_n), "
                "weight_kg = COALESCE(?, weight_kg), "
                "reps = COALESCE(?, reps), "
                "rpe = COALESCE(?, rpe), "
                "updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (
                    exercise,
                    session,
                    set_n,
                    weight_kg,
                    reps,
                    rpe,
                    now,
                    entry_id,
                    user_id,
                ),
            )
            cleared = await self._clear_fields(conn, "train", entry_id, user_id, clear)
            await conn.commit()
            cur = await conn.execute(SELECT_SQL["train"], (entry_id, user_id))
            after = await cur.fetchone()
        logger.info(
            "Train entry edited",
            user_id=user_id,
            entry_id=entry_id,
            before=dict(before),
            after=dict(after) if after else None,
            cleared=cleared,
        )
        return {"edited": True, "row": dict(after) if after else None}

    async def edit_activity(
        self,
        user_id: int,
        entry_id: int,
        type_: Optional[str] = None,
        duration_min: Optional[float] = None,
        intensity: Optional[str] = None,
        kcal_active: Optional[float] = None,
        kcal_total: Optional[float] = None,
        exercise_min: Optional[float] = None,
        stand_h: Optional[float] = None,
        steps: Optional[float] = None,
        distance_km: Optional[float] = None,
        source: Optional[str] = None,
        clear: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Correct one activity row in place. Same contract as edit_food.

        ``source`` is optional here (unlike ``log_activity``, where it is
        required): an edit is how a 'user_reported' placeholder gets upgraded
        to 'measured' once real data arrives, not a new fabrication surface.
        """
        now = _now()
        async with self._db.get_connection() as conn:
            cur = await conn.execute(SELECT_SQL["activity"], (entry_id, user_id))
            before = await cur.fetchone()
            if before is None:
                return {"edited": False, "reason": "no such entry for this user"}
            await conn.execute(
                "UPDATE logs_activity SET "
                "type = COALESCE(?, type), "
                "duration_min = COALESCE(?, duration_min), "
                "intensity = COALESCE(?, intensity), "
                "kcal_active = COALESCE(?, kcal_active), "
                "kcal_total = COALESCE(?, kcal_total), "
                "exercise_min = COALESCE(?, exercise_min), "
                "stand_h = COALESCE(?, stand_h), "
                "steps = COALESCE(?, steps), "
                "distance_km = COALESCE(?, distance_km), "
                "source = COALESCE(?, source), "
                "updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (
                    type_,
                    duration_min,
                    intensity,
                    kcal_active,
                    kcal_total,
                    exercise_min,
                    stand_h,
                    steps,
                    distance_km,
                    source,
                    now,
                    entry_id,
                    user_id,
                ),
            )
            cleared = await self._clear_fields(
                conn, "activity", entry_id, user_id, clear
            )
            await conn.commit()
            cur = await conn.execute(SELECT_SQL["activity"], (entry_id, user_id))
            after = await cur.fetchone()
        logger.info(
            "Activity entry edited",
            user_id=user_id,
            entry_id=entry_id,
            before=dict(before),
            after=dict(after) if after else None,
            cleared=cleared,
        )
        return {"edited": True, "row": dict(after) if after else None}

    async def delete_product(self, product_id: int) -> Dict[str, Any]:
        """Remove a shared product row.

        Products are shared by design (dv-5f3a), so this is NOT user-scoped —
        deleting one affects every profile that looks it up. That is why the
        row is journaled and why the tool description tells the agent to
        prefer correcting a product over removing it.
        """
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM products WHERE id = ?", (product_id,)
            )
            row = await cur.fetchone()
            if row is None:
                return {"deleted": False, "reason": "no such product"}
            await conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
            await conn.commit()
        removed = dict(row)
        logger.info("Product deleted", product_id=product_id, row=removed)
        return {"deleted": True, "row": removed}

    async def day_food_totals(self, user_id: int, date: str) -> Dict[str, Any]:
        """Computed day totals — SUM over NULLs stays NULL-safe (ignored)."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) AS meals, SUM(kcal) AS kcal, "
                "SUM(protein) AS protein, SUM(fat) AS fat, SUM(carb) AS carb "
                "FROM logs_food WHERE user_id = ? AND date = ?",
                (user_id, date),
            )
            row = await cur.fetchone()
        return dict(row) if row else {"meals": 0}

    # -- sleep --------------------------------------------------------------

    async def log_sleep(
        self,
        user_id: int,
        date: str,
        bed: Optional[str] = None,
        wake: Optional[str] = None,
        sleep_min: Optional[float] = None,
        onset_min: Optional[float] = None,
        score: Optional[float] = None,
        hrv: Optional[float] = None,
        rhr: Optional[float] = None,
        weight_kg: Optional[float] = None,
        wellbeing: Optional[str] = None,
        alcohol_u: Optional[float] = None,
        thc: Optional[str] = None,
        room_temp: Optional[float] = None,
        out_night: Optional[float] = None,
        out_day: Optional[float] = None,
        imported_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upsert on (user_id, date), provided fields only.

        COALESCE(excluded.col, existing.col): a field not provided in this
        call (NULL) keeps the stored value — fields can be filled in over the
        day (weight in the morning, bed time at night) without erasing each
        other. Returns the merged row plus 7-day averages.
        """
        result = await self.log_sleep_nights(
            user_id,
            [
                {
                    "date": date,
                    "bed": bed,
                    "wake": wake,
                    "sleep_min": sleep_min,
                    "onset_min": onset_min,
                    "score": score,
                    "hrv": hrv,
                    "rhr": rhr,
                    "weight_kg": weight_kg,
                    "wellbeing": wellbeing,
                    "alcohol_u": alcohol_u,
                    "thc": thc,
                    "room_temp": room_temp,
                    "out_night": out_night,
                    "out_day": out_day,
                    "imported_from": imported_from,
                }
            ],
        )
        night = result["nights"][0]
        return {"row": night["row"], "avg_7d": night["avg_7d"]}

    async def log_sleep_nights(
        self, user_id: int, nights: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Upsert many nights in a single transaction, one entry per date.

        dv-b042. Unlike food/train, ``date`` is per-row: a batch here is
        normally several *different* dates arriving together (a week read off
        a watch), not several rows for one day — the table is keyed
        (user_id, date), so two rows for the same date in one batch would
        merge into each other. The batch is deduped by date before writing so
        that merge is the caller's explicit choice rather than a surprise.

        The 7-day average is recomputed per distinct date AFTER every upsert
        lands, so a batch spanning a week reports averages that already
        include the whole week rather than a partial one.
        """
        now = _now()
        async with self._db.get_connection() as conn:
            for row in nights:
                await conn.execute(UPSERT_SLEEP_SQL, _params_sleep(user_id, row, now))
            await conn.commit()

            written: List[Dict[str, Any]] = []
            for date in _unique([row.get("date") for row in nights]):
                cur = await conn.execute(SELECT_SLEEP_DAY_SQL, (user_id, date))
                merged = await cur.fetchone()
                cur = await conn.execute(SELECT_SLEEP_AVG_SQL, (user_id, date, date))
                avgs = await cur.fetchone()
                written.append(
                    {
                        "date": date,
                        "row": dict(merged) if merged else None,
                        "avg_7d": dict(avgs) if avgs else None,
                    }
                )
        return {"written": len(written), "nights": written}

    async def edit_sleep_day(
        self,
        user_id: int,
        date: str,
        bed: Optional[str] = None,
        wake: Optional[str] = None,
        sleep_min: Optional[float] = None,
        onset_min: Optional[float] = None,
        score: Optional[float] = None,
        hrv: Optional[float] = None,
        rhr: Optional[float] = None,
        weight_kg: Optional[float] = None,
        wellbeing: Optional[str] = None,
        alcohol_u: Optional[float] = None,
        thc: Optional[str] = None,
        room_temp: Optional[float] = None,
        out_night: Optional[float] = None,
        out_day: Optional[float] = None,
        clear: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Correct one night in place, keyed on DATE (dv-4e81).

        Same contract as ``edit_food`` with one difference that follows from
        the table: ``logs_sleep`` is UNIQUE(user_id, date), so the address is
        the date rather than an entry_id — which is also how the agent thinks
        about a night, and it saves a ``day_summary`` round-trip just to learn
        an id.

        Why this exists at all: ``log_sleep`` upserts through
        COALESCE(excluded.col, existing.col), so passing NULL *keeps* the old
        value. That is right for "send only what you learned this turn", and
        it means a wrong number can be replaced but never blanked. The
        2026-08-19 repair had to null invented zeros out of score/hrv/rhr by
        hand; ``clear`` is the tool version of that, and the alternative the
        agent had — ``log_delete`` on the whole night — would have thrown away
        the real values sitting in the same row.
        """
        now = _now()
        async with self._db.get_connection() as conn:
            cur = await conn.execute(SELECT_SLEEP_DAY_SQL, (user_id, date))
            before = await cur.fetchone()
            if before is None:
                return {"edited": False, "reason": "no sleep entry for that date"}
            await conn.execute(
                UPDATE_SLEEP_SQL,
                (
                    bed,
                    wake,
                    sleep_min,
                    onset_min,
                    score,
                    hrv,
                    rhr,
                    weight_kg,
                    wellbeing,
                    alcohol_u,
                    thc,
                    room_temp,
                    out_night,
                    out_day,
                    now,
                    user_id,
                    date,
                ),
            )
            cleared: List[str] = []
            for name in clear or []:
                sql = CLEARABLE_SLEEP_SQL.get(str(name).strip().lower())
                if sql:
                    await conn.execute(sql, (user_id, date))
                    cleared.append(str(name).strip().lower())
            await conn.commit()
            cur = await conn.execute(SELECT_SLEEP_DAY_SQL, (user_id, date))
            after = await cur.fetchone()
            cur = await conn.execute(SELECT_SLEEP_AVG_SQL, (user_id, date, date))
            avgs = await cur.fetchone()
        logger.info(
            "Sleep entry edited",
            user_id=user_id,
            date=date,
            before=dict(before),
            after=dict(after) if after else None,
            cleared=cleared,
        )
        return {
            "edited": True,
            "date": date,
            "row": dict(after) if after else None,
            "avg_7d": dict(avgs) if avgs else None,
            "cleared": cleared,
        }

    async def missing_sleep_date_index(
        self, user_id: int, dates: List[str]
    ) -> Optional[int]:
        """Index of the first date with no night stored, or None if all exist.

        The dv-b042 pre-flight, dates instead of ids: a batch whose third
        date is a typo must not leave the first two applied, because the model
        would assume the whole batch landed.
        """
        async with self._db.get_connection() as conn:
            for index, date in enumerate(dates):
                cur = await conn.execute(SELECT_SLEEP_DAY_SQL, (user_id, date))
                if await cur.fetchone() is None:
                    return index
        return None

    async def edit_sleep_days(
        self, user_id: int, edits: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Apply several night corrections, dates pre-flighted as a set.

        NOT deduped by date (unlike ``log_sleep_nights``): edits are applied
        in the order given, so ``bad_index`` names the row the caller sent
        rather than a position in a rewritten list. Two edits addressing one
        night therefore stack in order, which is at least predictable.
        """
        missing = await self.missing_sleep_date_index(
            user_id, [str(e.get("date") or "") for e in edits]
        )
        if missing is not None:
            return {"edited": 0, "bad_index": missing}
        nights = [await self.edit_sleep_day(user_id, **edit) for edit in edits]
        return {"edited": len(nights), "nights": nights}

    async def sleep_last(self, user_id: int, n: int = 7) -> List[Dict[str, Any]]:
        limit = max(1, min(int(n), 60))
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM logs_sleep WHERE user_id = ? "
                "ORDER BY date DESC LIMIT ?",
                (user_id, limit),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- training -----------------------------------------------------------

    async def log_train(
        self,
        user_id: int,
        date: str,
        exercise: str,
        session: Optional[str] = None,
        set_n: Optional[str] = None,
        weight_kg: Optional[str] = None,
        reps: Optional[str] = None,
        rpe: Optional[float] = None,
        imported_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert one set row; returns today's sets for the exercise plus the
        previous session's sets (grounded progression — dv-7a3c failure
        class: history must come from data, never from the model)."""
        result = await self.log_train_sets(
            user_id,
            date,
            [
                {
                    "exercise": exercise,
                    "set_n": set_n,
                    "weight_kg": weight_kg,
                    "reps": reps,
                    "rpe": rpe,
                    "imported_from": imported_from,
                }
            ],
            session=session,
        )
        return {
            "id": result["ids"][0],
            "today": result["today"].get(exercise, []),
            "previous_session": result["previous_session"].get(
                exercise, {"date": None, "sets": []}
            ),
        }

    async def log_train_sets(
        self,
        user_id: int,
        date: str,
        sets: List[Dict[str, Any]],
        session: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert many set rows for one session in a single transaction.

        dv-b042: one set per call meant an ordinary workout cost 11+ sequential
        round-trips (2026-08-21, `pull_chest`, 54s of spinner). Worse than the
        latency, each call returned today's sets *plus* the previous session's
        for that exercise, so call 4 for an exercise re-returned calls 1-3 —
        quadratic context growth within a single exercise.

        The payload here is keyed by DISTINCT exercise: an eleven-set batch
        over four exercises returns four `today` lists and four
        `previous_session` blocks, each exactly once. The previous-session
        lookup still comes from SQL, never from the model (dv-7a3c).
        """
        now = _now()
        async with self._db.get_connection() as conn:
            ids: List[int] = []
            for row in sets:
                cur = await conn.execute(
                    INSERT_TRAIN_SQL, _params_train(user_id, date, session, row, now)
                )
                ids.append(int(cur.lastrowid or 0))
            await conn.commit()

            today: Dict[str, List[Dict[str, Any]]] = {}
            previous: Dict[str, Dict[str, Any]] = {}
            for exercise in _unique([row.get("exercise") for row in sets]):
                cur = await conn.execute(
                    SELECT_TRAIN_DAY_EXERCISE_SQL, (user_id, date, exercise)
                )
                today[exercise] = [dict(r) for r in await cur.fetchall()]
                cur = await conn.execute(
                    SELECT_TRAIN_PREV_DATE_SQL, (user_id, exercise, date)
                )
                prev = await cur.fetchone()
                prev_date = prev["date"] if prev else None
                prev_sets: List[Dict[str, Any]] = []
                if prev_date:
                    cur = await conn.execute(
                        SELECT_TRAIN_DAY_EXERCISE_SQL, (user_id, prev_date, exercise)
                    )
                    prev_sets = [dict(r) for r in await cur.fetchall()]
                previous[exercise] = {"date": prev_date, "sets": prev_sets}
        return {
            "written": len(ids),
            "ids": ids,
            "date": date,
            "session": session,
            "today": today,
            "previous_session": previous,
        }

    async def lift_history(
        self, user_id: int, exercise: str, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Set rows for the exercise over its most recent ``limit`` dates."""
        capped = max(1, min(int(limit), 30))
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM logs_train WHERE user_id = ? AND exercise = ? "
                "AND date IN (SELECT DISTINCT date FROM logs_train "
                "WHERE user_id = ? AND exercise = ? ORDER BY date DESC LIMIT ?) "
                "ORDER BY date DESC, id",
                (user_id, exercise, user_id, exercise, capped),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- activity -----------------------------------------------------------

    async def log_activity(
        self,
        user_id: int,
        date: str,
        type: str,
        source: str,
        duration_min: Optional[float] = None,
        intensity: Optional[str] = None,
        kcal_active: Optional[float] = None,
        kcal_total: Optional[float] = None,
        exercise_min: Optional[float] = None,
        stand_h: Optional[float] = None,
        steps: Optional[float] = None,
        distance_km: Optional[float] = None,
        imported_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert one activity row; returns the whole day's activity list.

        ``source`` is required, no default (dv-c388, mirrors
        ``save_product``'s ``provenance`` guard): 'user_reported' |
        'measured' | 'estimated'. The CHECK constraint is the backstop; the
        required parameter is what stops a call from skipping the choice.
        """
        result = await self.log_activities(
            user_id,
            date,
            [
                {
                    "type": type,
                    "source": source,
                    "duration_min": duration_min,
                    "intensity": intensity,
                    "kcal_active": kcal_active,
                    "kcal_total": kcal_total,
                    "exercise_min": exercise_min,
                    "stand_h": stand_h,
                    "steps": steps,
                    "distance_km": distance_km,
                    "imported_from": imported_from,
                }
            ],
        )
        return {"id": result["ids"][0], "day": result["day"]}

    async def log_activities(
        self, user_id: int, date: str, activities: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Insert many activity rows for one date in a single transaction.

        dv-b042. ``source`` per row is still mandatory and still unguessed —
        the tool layer rejects the whole batch before any statement runs, so
        an unlabelled row can never slip in behind a labelled one. The day's
        activity list is returned once, after every row is in.
        """
        now = _now()
        async with self._db.get_connection() as conn:
            ids: List[int] = []
            for row in activities:
                cur = await conn.execute(
                    INSERT_ACTIVITY_SQL, _params_activity(user_id, date, row, now)
                )
                ids.append(int(cur.lastrowid or 0))
            await conn.commit()
            cur = await conn.execute(SELECT_ACTIVITY_DAY_SQL, (user_id, date))
            day = [dict(r) for r in await cur.fetchall()]
        return {"written": len(ids), "ids": ids, "date": date, "day": day}

    # -- summaries ----------------------------------------------------------

    async def day_summary(self, user_id: int, date: str) -> Dict[str, Any]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM logs_food WHERE user_id = ? AND date = ? " "ORDER BY id",
                (user_id, date),
            )
            food = [dict(r) for r in await cur.fetchall()]
            cur = await conn.execute(
                "SELECT * FROM logs_train WHERE user_id = ? AND date = ? "
                "ORDER BY id",
                (user_id, date),
            )
            train = [dict(r) for r in await cur.fetchall()]
            cur = await conn.execute(
                "SELECT * FROM logs_activity WHERE user_id = ? AND date = ? "
                "ORDER BY id",
                (user_id, date),
            )
            activity = [dict(r) for r in await cur.fetchall()]
            cur = await conn.execute(
                "SELECT * FROM logs_sleep WHERE user_id = ? AND date = ?",
                (user_id, date),
            )
            sleep = await cur.fetchone()
        totals = await self.day_food_totals(user_id, date)
        return {
            "date": date,
            "food": food,
            "food_totals": totals,
            "train": train,
            "activity": activity,
            "sleep": dict(sleep) if sleep else None,
        }

    async def week_summary(
        self, user_id: int, end_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """Per-day kcal/meals plus training and activity volume, last 7 days.

        ``end_date`` defaults to the store's latest food/sleep date rather
        than the wall clock so replays and tests are deterministic.
        """
        async with self._db.get_connection() as conn:
            if end_date is None:
                cur = await conn.execute(
                    "SELECT MAX(date) AS d FROM (SELECT date FROM logs_food "
                    "WHERE user_id = ? UNION "
                    "SELECT date FROM logs_sleep WHERE user_id = ?)",
                    (user_id, user_id),
                )
                row = await cur.fetchone()
                end_date = row["d"] if row and row["d"] else None
            if end_date is None:
                return {"days": [], "end_date": None}
            cur = await conn.execute(
                "SELECT date, COUNT(*) AS meals, SUM(kcal) AS kcal, "
                "SUM(protein) AS protein FROM logs_food "
                "WHERE user_id = ? AND date >= date(?, '-6 days') "
                "AND date <= ? GROUP BY date ORDER BY date",
                (user_id, end_date, end_date),
            )
            food_days = [dict(r) for r in await cur.fetchall()]
            cur = await conn.execute(
                "SELECT date, COUNT(*) AS sets, "
                "COUNT(DISTINCT exercise) AS exercises FROM logs_train "
                "WHERE user_id = ? AND date >= date(?, '-6 days') "
                "AND date <= ? GROUP BY date ORDER BY date",
                (user_id, end_date, end_date),
            )
            train_days = [dict(r) for r in await cur.fetchall()]
            # dv-c388: an 'estimated' row must never harden into a TDEE
            # number — exclude it from the sum, and report what was excluded
            # rather than silently dropping it (a placeholder indistinguishable
            # from data at query time was the actual defect, not just the
            # missing disclaimer).
            cur = await conn.execute(
                "SELECT date, SUM(duration_min) AS duration_min, "
                "SUM(kcal_active) AS kcal_active_total "
                "FROM logs_activity "
                "WHERE user_id = ? AND date >= date(?, '-6 days') "
                "AND date <= ? AND source != 'estimated' "
                "GROUP BY date ORDER BY date",
                (user_id, end_date, end_date),
            )
            activity_days = [dict(r) for r in await cur.fetchall()]
            cur = await conn.execute(
                "SELECT date, COUNT(*) AS count, SUM(duration_min) AS duration_min "
                "FROM logs_activity "
                "WHERE user_id = ? AND date >= date(?, '-6 days') "
                "AND date <= ? AND source = 'estimated' "
                "GROUP BY date ORDER BY date",
                (user_id, end_date, end_date),
            )
            activity_estimated_excluded = [dict(r) for r in await cur.fetchall()]
        return {
            "end_date": end_date,
            "food": food_days,
            "train": train_days,
            "activity": activity_days,
            "activity_estimated_excluded": activity_estimated_excluded,
        }

    # -- day-state reads (dv-df45) ------------------------------------------

    async def latest_weight(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Most recent weigh-in of any age.

        Deliberately not bounded to a week like ``sleep_last``: the digest
        states when the weight was measured, so a stale value is informative
        rather than misleading, whereas omitting it hides that a weigh-in
        happened at all.
        """
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT date, weight_kg FROM logs_sleep "
                "WHERE user_id = ? AND weight_kg IS NOT NULL "
                "ORDER BY date DESC LIMIT 1",
                (user_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def last_train_before(
        self, user_id: int, date: str
    ) -> Optional[Dict[str, Any]]:
        """The most recent training day STRICTLY before ``date``.

        Sessions are many rows per day; this collapses to the day and the
        session label so the digest can say "last session before today".
        """
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT date, session, COUNT(*) AS sets FROM logs_train "
                "WHERE user_id = ? AND date < ? "
                "GROUP BY date ORDER BY date DESC LIMIT 1",
                (user_id, date),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def has_entries_on(self, user_id: int, date: str) -> bool:
        """True when ANY of the four logs holds a row for ``date`` — the
        gap-ping criterion (owner: talking is not logging)."""
        queries = (
            "SELECT 1 FROM logs_food WHERE user_id = ? AND date = ? LIMIT 1",
            "SELECT 1 FROM logs_sleep WHERE user_id = ? AND date = ? LIMIT 1",
            "SELECT 1 FROM logs_train WHERE user_id = ? AND date = ? LIMIT 1",
            "SELECT 1 FROM logs_activity WHERE user_id = ? AND date = ? LIMIT 1",
        )
        async with self._db.get_connection() as conn:
            for sql in queries:
                cur = await conn.execute(sql, (user_id, date))
                if await cur.fetchone():
                    return True
        return False

    # -- products (shared by design) ---------------------------------------

    async def save_product(
        self,
        name: str,
        kind: str,
        provenance: str,
        brand: str = "",
        kcal_100g: Optional[float] = None,
        protein: Optional[float] = None,
        fat: Optional[float] = None,
        carb: Optional[float] = None,
        salt: Optional[float] = None,
        confirmed_on: Optional[str] = None,
        created_by: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Upsert on (name, brand) with the provenance guard.

        An ``estimate`` never overwrites a ``label_photo``/``official`` row:
        the existing row is returned with ``saved: False`` and the caller
        (agent) sees the authoritative values instead. Provenance has no
        default and is never inherited.
        """
        now = _now()
        async with self._db.get_connection() as conn:
            # Resolve an existing row by folded key, then upsert under the
            # name already stored. ON CONFLICT(name, brand) is bound to the
            # case-sensitive UNIQUE index, so writing "зефир" next to a
            # stored "Зефир" would insert a second row rather than update
            # it: a duplicate the provenance guard cannot see past and the
            # agent then picks between at random.
            cur = await conn.execute(
                "SELECT * FROM products "
                "WHERE py_casefold(name) = ? AND py_casefold(brand) = ?",
                (_casefold(name), _casefold(brand)),
            )
            existing = await cur.fetchone()
            if existing is not None:
                name, brand = existing["name"], existing["brand"]
            if (
                existing is not None
                and provenance == "estimate"
                and existing["provenance"] in ("label_photo", "official")
            ):
                logger.info(
                    "Product estimate refused — authoritative row exists",
                    name=name,
                    brand=brand,
                    existing_provenance=existing["provenance"],
                )
                return {
                    "saved": False,
                    "reason": "estimate_never_overwrites",
                    "row": dict(existing),
                }
            await conn.execute(
                "INSERT INTO products (name, brand, kind, kcal_100g, protein, "
                "fat, carb, salt, provenance, confirmed_on, created_by, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name, brand) DO UPDATE SET "
                "kind = excluded.kind, "
                "kcal_100g = COALESCE(excluded.kcal_100g, products.kcal_100g), "
                "protein = COALESCE(excluded.protein, products.protein), "
                "fat = COALESCE(excluded.fat, products.fat), "
                "carb = COALESCE(excluded.carb, products.carb), "
                "salt = COALESCE(excluded.salt, products.salt), "
                "provenance = excluded.provenance, "
                "confirmed_on = COALESCE(excluded.confirmed_on, "
                "products.confirmed_on), "
                "updated_at = excluded.updated_at",
                (
                    name,
                    brand,
                    kind,
                    kcal_100g,
                    protein,
                    fat,
                    carb,
                    salt,
                    provenance,
                    confirmed_on,
                    created_by,
                    now,
                    now,
                ),
            )
            await conn.commit()
            cur = await conn.execute(
                "SELECT * FROM products WHERE name = ? AND brand = ?",
                (name, brand),
            )
            row = await cur.fetchone()
        return {"saved": True, "row": dict(row) if row else None}

    async def save_products(self, products: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Save several products, each through the single-row provenance guard.

        dv-b042: reading one label photo or one menu yields several products
        at once. Deliberately NOT one transaction — ``save_product`` refuses
        an ``estimate`` that would overwrite ``label_photo``/``official`` data
        and returns the authoritative row instead, and that refusal is a
        per-product answer, not a batch failure. Rolling back the eight good
        products because the ninth already had better data would throw away
        real work; the result list reports each outcome separately.
        """
        results = [await self.save_product(**product) for product in products]
        return {
            "written": sum(1 for r in results if r.get("saved")),
            "refused": sum(1 for r in results if not r.get("saved")),
            "results": results,
        }

    async def product_find(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Substring match on name/brand, case-insensitive including Cyrillic.

        Provenance is always in the payload so an estimate can never
        masquerade as label data downstream.

        Case matters here more than it looks: a miss does not read as "no
        data" downstream, it reads as "not in the base yet", and the agent
        then re-estimates a product whose label it already photographed. The
        numbers for the same food drift between meals as a result.
        """
        capped = max(1, min(int(limit), 25))
        pattern = "%" + query.strip().casefold() + "%"
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM products "
                "WHERE py_casefold(name) LIKE ? OR py_casefold(brand) LIKE ? "
                "ORDER BY name LIMIT ?",
                (pattern, pattern, capped),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- portions (per-user) ------------------------------------------------

    async def save_portion(
        self,
        user_id: int,
        phrase: str,
        grams: float,
        learned_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Per-user portion phrase upsert ("нормальная пицца" -> grams)."""
        now = _now()
        async with self._db.get_connection() as conn:
            # Same folded-key resolve as save_product: "Нормальная пицца" and
            # "нормальная пицца" are one learned portion, not two.
            cur = await conn.execute(
                "SELECT phrase FROM portions "
                "WHERE user_id = ? AND py_casefold(phrase) = ?",
                (user_id, _casefold(phrase)),
            )
            existing = await cur.fetchone()
            if existing is not None:
                phrase = existing["phrase"]
            await conn.execute(
                "INSERT INTO portions (user_id, phrase, grams, learned_from, "
                "updated_on, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, phrase) DO UPDATE SET "
                "grams = excluded.grams, "
                "learned_from = COALESCE(excluded.learned_from, "
                "portions.learned_from), "
                "updated_on = excluded.updated_on, "
                "updated_at = excluded.updated_at",
                (user_id, phrase, grams, learned_from, now, now, now),
            )
            await conn.commit()
            cur = await conn.execute(
                "SELECT * FROM portions WHERE user_id = ? AND phrase = ?",
                (user_id, phrase),
            )
            row = await cur.fetchone()
        return dict(row) if row else {}

    async def save_portions(
        self, user_id: int, portions: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Upsert several portion phrases for this user (dv-b042)."""
        rows = [await self.save_portion(user_id, **portion) for portion in portions]
        return {"written": len(rows), "rows": rows}

    async def portion_find(
        self, user_id: int, phrase: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        capped = max(1, min(int(limit), 25))
        pattern = "%" + phrase.strip().casefold() + "%"
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM portions WHERE user_id = ? "
                "AND py_casefold(phrase) LIKE ? "
                "ORDER BY phrase LIMIT ?",
                (user_id, pattern, capped),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_portion(self, user_id: int, portion_id: int) -> Dict[str, Any]:
        """Remove one of THIS user's portion phrases (dv-4e81).

        User-scoped, unlike ``delete_product``: portions are per-user, so the
        ``AND user_id = ?`` means another user's id simply matches nothing.
        ``save_portion`` can already overwrite a phrase's grams, so this is
        only for a phrase that should never have been learned — a mis-heard
        phrase, or grams attributed to the wrong food.
        """
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM portions WHERE id = ? AND user_id = ?",
                (portion_id, user_id),
            )
            row = await cur.fetchone()
            if row is None:
                return {"deleted": False, "reason": "no such portion for this user"}
            await conn.execute(
                "DELETE FROM portions WHERE id = ? AND user_id = ?",
                (portion_id, user_id),
            )
            await conn.commit()
        removed = dict(row)
        logger.info(
            "Portion deleted", user_id=user_id, portion_id=portion_id, row=removed
        )
        return {"deleted": True, "row": removed}

    # -- issues (consumers: dv-b9c7) ----------------------------------------

    async def issue_open(
        self,
        user_id: int,
        title: str,
        status: str = "ACTIVE",
        started_on: Optional[str] = None,
        symptom: Optional[str] = None,
        restrictions: Optional[str] = None,
        escalation: Optional[str] = None,
        notes: Optional[str] = None,
        imported_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = _now()
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "INSERT INTO issues (user_id, title, status, started_on, "
                "symptom, restrictions, escalation, notes, imported_from, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    title,
                    status,
                    started_on,
                    symptom,
                    restrictions,
                    escalation,
                    notes,
                    imported_from,
                    now,
                    now,
                ),
            )
            await conn.commit()
            row_id = int(cur.lastrowid or 0)
            cur = await conn.execute(
                "SELECT * FROM issues WHERE id = ? AND user_id = ?",
                (row_id, user_id),
            )
            row = await cur.fetchone()
        return dict(row) if row else {}

    async def issue_update(
        self,
        user_id: int,
        issue_id: int,
        status: Optional[str] = None,
        resolved_on: Optional[str] = None,
        restrictions: Optional[str] = None,
        escalation: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Ownership check mirrors MemoryRepository.get_fact: a wrong user_id
        behaves as not-found, never as another user's row."""
        now = _now()
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "UPDATE issues SET "
                "status = COALESCE(?, status), "
                "resolved_on = COALESCE(?, resolved_on), "
                "restrictions = COALESCE(?, restrictions), "
                "escalation = COALESCE(?, escalation), "
                "notes = COALESCE(?, notes), "
                "updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (
                    status,
                    resolved_on,
                    restrictions,
                    escalation,
                    notes,
                    now,
                    issue_id,
                    user_id,
                ),
            )
            await conn.commit()
            if cur.rowcount == 0:
                return None
            cur = await conn.execute(
                "SELECT * FROM issues WHERE id = ? AND user_id = ?",
                (issue_id, user_id),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def issue_list(
        self, user_id: int, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM issues WHERE user_id = ? "
                "AND (? IS NULL OR status = ?) ORDER BY id",
                (user_id, status, status),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- docs ---------------------------------------------------------------

    async def doc_save(
        self,
        user_id: int,
        kind: str,
        content: str,
        period: Optional[str] = None,
        title: Optional[str] = None,
        imported_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = _now()
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "INSERT INTO docs (user_id, kind, period, title, content, "
                "imported_from, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, kind, period, title, content, imported_from, now, now),
            )
            await conn.commit()
            row_id = int(cur.lastrowid or 0)
        return {"id": row_id, "kind": kind, "period": period, "title": title}

    async def doc_get(self, user_id: int, doc_id: int) -> Optional[Dict[str, Any]]:
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM docs WHERE id = ? AND user_id = ?",
                (doc_id, user_id),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def doc_list(
        self,
        user_id: int,
        kind: Optional[str] = None,
        period: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Listing carries no content — docs can be long; fetch via doc_get."""
        async with self._db.get_connection() as conn:
            cur = await conn.execute(
                "SELECT id, kind, period, title, imported_from, created_at, "
                "updated_at, length(content) AS content_chars "
                "FROM docs WHERE user_id = ? "
                "AND (? IS NULL OR kind = ?) AND (? IS NULL OR period = ?) "
                "ORDER BY id",
                (user_id, kind, kind, period, period),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- export (lane 2: agent analysis over per-user extracts) --------------

    # Domain -> static SQL. The date window applies where a date column
    # exists; portions/issues/docs return the user's full set. products is
    # the one shared table (data scope, not access control).
    _EXPORT_DATED = {
        "sleep": (
            "SELECT * FROM logs_sleep WHERE user_id = ? "
            "AND (? IS NULL OR date >= ?) AND (? IS NULL OR date <= ?) "
            "ORDER BY date, id"
        ),
        "food": (
            "SELECT * FROM logs_food WHERE user_id = ? "
            "AND (? IS NULL OR date >= ?) AND (? IS NULL OR date <= ?) "
            "ORDER BY date, id"
        ),
        "train": (
            "SELECT * FROM logs_train WHERE user_id = ? "
            "AND (? IS NULL OR date >= ?) AND (? IS NULL OR date <= ?) "
            "ORDER BY date, id"
        ),
        "activity": (
            "SELECT * FROM logs_activity WHERE user_id = ? "
            "AND (? IS NULL OR date >= ?) AND (? IS NULL OR date <= ?) "
            "ORDER BY date, id"
        ),
    }
    _EXPORT_PLAIN = {
        "portions": "SELECT * FROM portions WHERE user_id = ? ORDER BY phrase",
        "issues": "SELECT * FROM issues WHERE user_id = ? ORDER BY id",
        "docs": "SELECT * FROM docs WHERE user_id = ? ORDER BY id",
    }
    _EXPORT_SHARED = {"products": "SELECT * FROM products ORDER BY name, brand"}

    EXPORT_DOMAINS = (
        frozenset(_EXPORT_DATED) | frozenset(_EXPORT_PLAIN) | frozenset(_EXPORT_SHARED)
    )

    async def export_rows(
        self,
        user_id: int,
        domain: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Raw rows of one domain for ONE user (products: shared, unscoped).

        The universal read path: anything the shaped read tools don't answer,
        the agent exports and analyzes as files. Unknown domain raises
        ValueError — callers validate, this is the backstop.
        """
        if domain in self._EXPORT_DATED:
            query = self._EXPORT_DATED[domain]
            params: Tuple[Any, ...] = (
                user_id,
                date_from,
                date_from,
                date_to,
                date_to,
            )
        elif domain in self._EXPORT_PLAIN:
            query = self._EXPORT_PLAIN[domain]
            params = (user_id,)
        elif domain in self._EXPORT_SHARED:
            query = self._EXPORT_SHARED[domain]
            params = ()
        else:
            raise ValueError(f"unknown export domain: {domain}")
        async with self._db.get_connection() as conn:
            cur = await conn.execute(query, params)
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- migration support ---------------------------------------------------

    async def delete_imported(self, user_id: int, imported_from: str) -> int:
        """Remove rows a specific CSV import created — the migration's
        idempotence and revert lever. Live rows (imported_from NULL) are
        untouchable through this path."""
        total = 0
        async with self._db.get_connection() as conn:
            for stmt in (
                "DELETE FROM logs_sleep WHERE user_id = ? AND imported_from = ?",
                "DELETE FROM logs_food WHERE user_id = ? AND imported_from = ?",
                "DELETE FROM logs_train WHERE user_id = ? AND imported_from = ?",
                "DELETE FROM logs_activity WHERE user_id = ? AND imported_from = ?",
                "DELETE FROM issues WHERE user_id = ? AND imported_from = ?",
                "DELETE FROM docs WHERE user_id = ? AND imported_from = ?",
            ):
                cur = await conn.execute(stmt, (user_id, imported_from))
                total += cur.rowcount or 0
            await conn.commit()
        return total

    async def count_imported(self, user_id: int, imported_from: str) -> Dict[str, int]:
        """Per-table row counts for one import source — the verification key
        (CSV row count must equal these, dv-77cd lesson)."""
        counts: Dict[str, int] = {}
        async with self._db.get_connection() as conn:
            for table, stmt in (
                (
                    "logs_sleep",
                    "SELECT COUNT(*) FROM logs_sleep WHERE user_id = ? AND imported_from = ?",
                ),
                (
                    "logs_food",
                    "SELECT COUNT(*) FROM logs_food WHERE user_id = ? AND imported_from = ?",
                ),
                (
                    "logs_train",
                    "SELECT COUNT(*) FROM logs_train WHERE user_id = ? AND imported_from = ?",
                ),
                (
                    "logs_activity",
                    "SELECT COUNT(*) FROM logs_activity WHERE user_id = ? AND imported_from = ?",
                ),
                (
                    "issues",
                    "SELECT COUNT(*) FROM issues WHERE user_id = ? AND imported_from = ?",
                ),
                (
                    "docs",
                    "SELECT COUNT(*) FROM docs WHERE user_id = ? AND imported_from = ?",
                ),
            ):
                cur = await conn.execute(stmt, (user_id, imported_from))
                row = await cur.fetchone()
                counts[table] = int(row[0]) if row else 0
        return counts
