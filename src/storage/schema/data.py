"""Schema of data.db (UserDataManager) as SQLAlchemy Core tables (dv-5e4f Phase 1).

Source of truth for Alembic autogenerate (``alembic -n data revision
--autogenerate``). Queries stay raw SQL; nothing imports these tables to
build statements.

Rules that keep prod compatible (enforced by ``scripts/schema_diff.py`` and
``tests/unit/test_storage/test_alembic_schema.py``):

- Column ORDER equals prod: many sites use ``SELECT *`` / ``Model(*row)``.
- Declared type names render exactly (``TIMESTAMP``/``DATETIME``/``DATE``/
  ``BOOLEAN``/``JSON``): ``detect_types=PARSE_DECLTYPES`` converters key on
  them. Use the uppercase SQLAlchemy types, never generic ``DateTime`` etc.
- Primary-key ``nullable`` is explicit, mirroring prod's NOT NULL flags.
- ``sqlite_autoincrement=True`` wherever prod says AUTOINCREMENT; CHECKs
  and ``server_default`` are spelled out verbatim.

``py_casefold`` is a per-connection function hook, not schema
(``UserDataManager._new_connection``).
"""

from sqlalchemy import (
    INTEGER,
    REAL,
    TEXT,
    CheckConstraint,
    Column,
    Index,
    MetaData,
    Table,
    UniqueConstraint,
    text,
)

metadata = MetaData()


docs = Table(
    "docs",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("user_id", INTEGER, nullable=False),
    Column("kind", TEXT, nullable=False),
    Column("period", TEXT),
    Column("title", TEXT),
    Column("content", TEXT, nullable=False),
    Column("imported_from", TEXT),
    Column("created_at", TEXT, nullable=False),
    Column("updated_at", TEXT, nullable=False),
    CheckConstraint("kind IN ('note','program')"),
    Index("idx_docs_user_kind", "user_id", "kind", "period"),
    sqlite_autoincrement=True,
)

issues = Table(
    "issues",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("user_id", INTEGER, nullable=False),
    Column("title", TEXT, nullable=False),
    Column("status", TEXT, nullable=False),
    Column("started_on", TEXT),
    Column("resolved_on", TEXT),
    Column("symptom", TEXT),
    Column("restrictions", TEXT),
    Column("escalation", TEXT),
    Column("notes", TEXT),
    Column("imported_from", TEXT),
    Column("created_at", TEXT, nullable=False),
    Column("updated_at", TEXT, nullable=False),
    CheckConstraint("status IN ('ACTIVE','RESOLVED','CHRONIC')"),
    Index("idx_issues_user_status", "user_id", "status"),
    sqlite_autoincrement=True,
)

logs_activity = Table(
    "logs_activity",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("user_id", INTEGER, nullable=False),
    Column("date", TEXT, nullable=False),
    Column("type", TEXT, nullable=False),
    Column("duration_min", REAL),
    Column("intensity", TEXT),
    Column("source", TEXT, nullable=False),
    Column("imported_from", TEXT),
    Column("created_at", TEXT, nullable=False),
    Column("updated_at", TEXT, nullable=False),
    Column("kcal_active", REAL),
    Column("kcal_total", REAL),
    Column("exercise_min", REAL),
    Column("stand_h", REAL),
    Column("steps", REAL),
    Column("distance_km", REAL),
    CheckConstraint("source IN ('user_reported','measured','estimated')"),
    Index("idx_logs_activity_user_date", "user_id", "date"),
    sqlite_autoincrement=True,
)

logs_food = Table(
    "logs_food",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("user_id", INTEGER, nullable=False),
    Column("date", TEXT, nullable=False),
    Column("meal", TEXT, nullable=False),
    Column("time", TEXT),
    Column("kcal", REAL),
    Column("protein", REAL),
    Column("fat", REAL),
    Column("carb", REAL),
    Column("confidence", TEXT),
    Column("source", TEXT),
    Column("imported_from", TEXT),
    Column("created_at", TEXT, nullable=False),
    Column("updated_at", TEXT, nullable=False),
    Index("idx_logs_food_user_date", "user_id", "date"),
    sqlite_autoincrement=True,
)

logs_sleep = Table(
    "logs_sleep",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("user_id", INTEGER, nullable=False),
    Column("date", TEXT, nullable=False),
    Column("bed", TEXT),
    Column("wake", TEXT),
    Column("sleep_min", REAL),
    Column("onset_min", REAL),
    Column("score", REAL),
    Column("hrv", REAL),
    Column("rhr", REAL),
    Column("weight_kg", REAL),
    Column("wellbeing", TEXT),
    Column("alcohol_u", REAL),
    Column("thc", TEXT),
    Column("room_temp", REAL),
    Column("out_night", REAL),
    Column("out_day", REAL),
    Column("imported_from", TEXT),
    Column("created_at", TEXT, nullable=False),
    Column("updated_at", TEXT, nullable=False),
    UniqueConstraint("user_id", "date"),
    sqlite_autoincrement=True,
)

logs_train = Table(
    "logs_train",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("user_id", INTEGER, nullable=False),
    Column("date", TEXT, nullable=False),
    Column("session", TEXT),
    Column("exercise", TEXT, nullable=False),
    Column("set_n", TEXT),
    Column("weight_kg", TEXT),
    Column("reps", TEXT),
    Column("rpe", REAL),
    Column("imported_from", TEXT),
    Column("created_at", TEXT, nullable=False),
    Column("updated_at", TEXT, nullable=False),
    Index("idx_logs_train_user_date", "user_id", "date"),
    Index("idx_logs_train_user_exercise", "user_id", "exercise"),
    sqlite_autoincrement=True,
)

portions = Table(
    "portions",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("user_id", INTEGER, nullable=False),
    Column("phrase", TEXT, nullable=False),
    Column("grams", REAL, nullable=False),
    Column("learned_from", TEXT),
    Column("updated_on", TEXT),
    Column("created_at", TEXT, nullable=False),
    Column("updated_at", TEXT, nullable=False),
    UniqueConstraint("user_id", "phrase"),
    sqlite_autoincrement=True,
)

products = Table(
    "products",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("name", TEXT, nullable=False),
    Column("brand", TEXT, nullable=False, server_default=text("''")),
    Column("kind", TEXT, nullable=False),
    Column("kcal_100g", REAL),
    Column("protein", REAL),
    Column("fat", REAL),
    Column("carb", REAL),
    Column("salt", REAL),
    Column("provenance", TEXT, nullable=False),
    Column("confirmed_on", TEXT),
    Column("created_by", INTEGER),
    Column("created_at", TEXT, nullable=False),
    Column("updated_at", TEXT, nullable=False),
    CheckConstraint("kind IN ('product','dish')"),
    CheckConstraint("provenance IN ('label_photo','official','estimate')"),
    UniqueConstraint("name", "brand"),
    sqlite_autoincrement=True,
)
