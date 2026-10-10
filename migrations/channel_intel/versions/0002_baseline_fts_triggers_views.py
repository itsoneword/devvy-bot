"""baseline FTS5, triggers, views (raw SQL)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10

Objects Alembic cannot model: places_fts / images_fts / refs_fts (FTS5) and their 9 sync triggers.
SQL copied verbatim from the legacy-built schema's sqlite_master, so prod
and fresh installs compare equal (scripts/schema_diff.py). Frozen: change
these objects in a NEW revision, never here.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FTS_TABLES = [
    """CREATE VIRTUAL TABLE images_fts USING fts5(caption, tags, content='place_images', content_rowid='id')""",
    """CREATE VIRTUAL TABLE places_fts USING fts5(name, my_notes, location, content='places', content_rowid='id')""",
    """CREATE VIRTUAL TABLE refs_fts USING fts5(text, notes, content='place_references', content_rowid='id')""",
]

TRIGGERS = [
    """CREATE TRIGGER place_images_ad AFTER DELETE ON place_images BEGIN
  INSERT INTO images_fts(images_fts, rowid, caption, tags)
  VALUES ('delete', old.id, old.caption, old.tags);
END""",
    """CREATE TRIGGER place_images_ai AFTER INSERT ON place_images BEGIN
  INSERT INTO images_fts(rowid, caption, tags)
  VALUES (new.id, new.caption, new.tags);
END""",
    """CREATE TRIGGER place_images_au AFTER UPDATE ON place_images BEGIN
  INSERT INTO images_fts(images_fts, rowid, caption, tags)
  VALUES ('delete', old.id, old.caption, old.tags);
  INSERT INTO images_fts(rowid, caption, tags)
  VALUES (new.id, new.caption, new.tags);
END""",
    """CREATE TRIGGER place_references_ad AFTER DELETE ON place_references BEGIN
  INSERT INTO refs_fts(refs_fts, rowid, text, notes)
  VALUES ('delete', old.id, old.text, old.notes);
END""",
    """CREATE TRIGGER place_references_ai AFTER INSERT ON place_references BEGIN
  INSERT INTO refs_fts(rowid, text, notes)
  VALUES (new.id, new.text, new.notes);
END""",
    """CREATE TRIGGER place_references_au AFTER UPDATE ON place_references BEGIN
  INSERT INTO refs_fts(refs_fts, rowid, text, notes)
  VALUES ('delete', old.id, old.text, old.notes);
  INSERT INTO refs_fts(rowid, text, notes)
  VALUES (new.id, new.text, new.notes);
END""",
    """CREATE TRIGGER places_ad AFTER DELETE ON places BEGIN
  INSERT INTO places_fts(places_fts, rowid, name, my_notes, location)
  VALUES ('delete', old.id, old.name, old.my_notes, old.location);
END""",
    """CREATE TRIGGER places_ai AFTER INSERT ON places BEGIN
  INSERT INTO places_fts(rowid, name, my_notes, location)
  VALUES (new.id, new.name, new.my_notes, new.location);
END""",
    """CREATE TRIGGER places_au AFTER UPDATE ON places BEGIN
  INSERT INTO places_fts(places_fts, rowid, name, my_notes, location)
  VALUES ('delete', old.id, old.name, old.my_notes, old.location);
  INSERT INTO places_fts(rowid, name, my_notes, location)
  VALUES (new.id, new.name, new.my_notes, new.location);
END""",
]

VIEWS = []


def upgrade() -> None:
    for sql in FTS_TABLES + TRIGGERS + VIEWS:
        op.execute(sql)


_VIEW_NAMES = []
_TRIGGER_NAMES = [
    "place_images_ad",
    "place_images_ai",
    "place_images_au",
    "place_references_ad",
    "place_references_ai",
    "place_references_au",
    "places_ad",
    "places_ai",
    "places_au",
]
_FTS_NAMES = ["images_fts", "places_fts", "refs_fts"]


def downgrade() -> None:
    for view in _VIEW_NAMES:
        op.execute(f"DROP VIEW IF EXISTS {view}")
    for trigger in _TRIGGER_NAMES:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in _FTS_NAMES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
