"""Schema of channel_intel.db (channel KB) as SQLAlchemy Core tables (dv-5e4f Phase 1).

Source of truth for Alembic autogenerate (``alembic -n channel_intel revision
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

Not modelled here (raw SQL in baseline revision ``0002``): the
``places_fts``/``images_fts``/``refs_fts`` FTS5 tables + shadow tables and
the 9 sync triggers.
"""

from sqlalchemy import (
    INTEGER,
    REAL,
    TEXT,
    TIMESTAMP,
    Column,
    ForeignKeyConstraint,
    MetaData,
    Table,
    text,
)

metadata = MetaData()


comments = Table(
    "comments",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("owner", TEXT, nullable=False, server_default=text("'me'")),
    Column("user_id", INTEGER),
    Column("post_ref", TEXT),
    Column("text", TEXT),
    Column("topics", TEXT),
    Column("sentiment", TEXT),
    Column("created_at", TIMESTAMP),
    ForeignKeyConstraint(["user_id"], ["kb_users.id"]),
    sqlite_autoincrement=True,
)

kb_users = Table(
    "kb_users",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("owner", TEXT, nullable=False, server_default=text("'me'")),
    Column("tg_user_id", INTEGER),
    Column("username", TEXT),
    Column("display_name", TEXT),
    Column("first_seen", TIMESTAMP),
    sqlite_autoincrement=True,
)

place_images = Table(
    "place_images",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("owner", TEXT, nullable=False, server_default=text("'me'")),
    Column("place_id", INTEGER),
    Column("file_path", TEXT, nullable=False),
    Column("caption", TEXT),
    Column("tags", TEXT),
    Column("vision_model", TEXT),
    Column("confirmed", INTEGER, nullable=False, server_default=text("0")),
    Column("created_at", TIMESTAMP, nullable=False),
    ForeignKeyConstraint(["place_id"], ["places.id"]),
    sqlite_autoincrement=True,
)

place_references = Table(
    "place_references",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("owner", TEXT, nullable=False, server_default=text("'me'")),
    Column("source_channel", TEXT),
    Column("text", TEXT),
    Column("image_refs", TEXT),
    Column("tags", TEXT),
    Column("notes", TEXT),
    Column("created_at", TIMESTAMP, nullable=False),
    sqlite_autoincrement=True,
)

places = Table(
    "places",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("owner", TEXT, nullable=False, server_default=text("'me'")),
    Column("name", TEXT, nullable=False),
    Column("type", TEXT),
    Column("location", TEXT),
    Column("lat", REAL),
    Column("lon", REAL),
    Column("travel_time_from_home", TEXT),
    Column("price", TEXT),
    Column("opening_hours", TEXT),
    Column("features", TEXT),
    Column("status", TEXT, nullable=False, server_default=text("'idea'")),
    Column("liked", INTEGER),
    Column("my_notes", TEXT),
    Column("source", TEXT),
    Column("enriched_at", TIMESTAMP),
    Column("created_at", TIMESTAMP, nullable=False),
    Column("updated_at", TIMESTAMP, nullable=False),
    sqlite_autoincrement=True,
)

posts = Table(
    "posts",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("owner", TEXT, nullable=False, server_default=text("'me'")),
    Column("place_ids", TEXT),
    Column("body", TEXT),
    Column("selected_image_refs", TEXT),
    Column("status", TEXT, nullable=False, server_default=text("'draft'")),
    Column("created_at", TIMESTAMP, nullable=False),
    Column("updated_at", TIMESTAMP, nullable=False),
    sqlite_autoincrement=True,
)

preferences = Table(
    "preferences",
    metadata,
    Column("owner", TEXT, primary_key=True, nullable=True),
    Column("liked_features", TEXT),
    Column("liked_place_ids", TEXT),
    Column("budget_norms", TEXT),
    Column("home_location", TEXT),
    Column("default_radius", TEXT),
)

reference_places = Table(
    "reference_places",
    metadata,
    Column("reference_id", INTEGER, primary_key=True, nullable=False),
    Column("place_id", INTEGER, primary_key=True, nullable=False),
    ForeignKeyConstraint(["reference_id"], ["place_references.id"]),
    ForeignKeyConstraint(["place_id"], ["places.id"]),
)

visits = Table(
    "visits",
    metadata,
    Column("id", INTEGER, primary_key=True, nullable=True),
    Column("owner", TEXT, nullable=False, server_default=text("'me'")),
    Column("place_id", INTEGER, nullable=False),
    Column("date", TEXT),
    Column("activities", TEXT),
    Column("costs", TEXT),
    Column("narrative", TEXT),
    Column("photo_refs", TEXT),
    Column("created_at", TIMESTAMP, nullable=False),
    ForeignKeyConstraint(["place_id"], ["places.id"]),
    sqlite_autoincrement=True,
)
