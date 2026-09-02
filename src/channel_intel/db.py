"""Channel KB database: own SQLite file, separate from the bot DB."""

import sqlite3
from datetime import datetime
from pathlib import Path

import aiosqlite

sqlite3.register_adapter(datetime, lambda v: v.isoformat())
sqlite3.register_converter("TIMESTAMP", lambda b: datetime.fromisoformat(b.decode()))

SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'me',
    name TEXT NOT NULL,
    type TEXT,
    location TEXT,
    lat REAL, lon REAL,
    travel_time_from_home TEXT,
    price TEXT,
    opening_hours TEXT,
    features TEXT,
    status TEXT NOT NULL DEFAULT 'idea',
    liked INTEGER,
    my_notes TEXT,
    source TEXT,
    enriched_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS place_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'me',
    place_id INTEGER,
    file_path TEXT NOT NULL,
    caption TEXT,
    tags TEXT,
    vision_model TEXT,
    confirmed INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL,
    FOREIGN KEY (place_id) REFERENCES places(id)
);
CREATE TABLE IF NOT EXISTS place_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'me',
    source_channel TEXT,
    text TEXT,
    image_refs TEXT,
    tags TEXT,
    notes TEXT,
    created_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS reference_places (
    reference_id INTEGER NOT NULL,
    place_id INTEGER NOT NULL,
    PRIMARY KEY (reference_id, place_id),
    FOREIGN KEY (reference_id) REFERENCES place_references(id),
    FOREIGN KEY (place_id) REFERENCES places(id)
);
CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'me',
    place_id INTEGER NOT NULL,
    date TEXT,
    activities TEXT,
    costs TEXT,
    narrative TEXT,
    photo_refs TEXT,
    created_at TIMESTAMP NOT NULL,
    FOREIGN KEY (place_id) REFERENCES places(id)
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'me',
    place_ids TEXT,
    body TEXT,
    selected_image_refs TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
    owner TEXT PRIMARY KEY,
    liked_features TEXT,
    liked_place_ids TEXT,
    budget_norms TEXT,
    home_location TEXT,
    default_radius TEXT
);
CREATE TABLE IF NOT EXISTS kb_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'me',
    tg_user_id INTEGER, username TEXT, display_name TEXT, first_seen TIMESTAMP
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT 'me',
    user_id INTEGER, post_ref TEXT, text TEXT,
    topics TEXT, sentiment TEXT, created_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES kb_users(id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS places_fts USING fts5(name, my_notes, location, content='places', content_rowid='id');
CREATE VIRTUAL TABLE IF NOT EXISTS images_fts USING fts5(caption, tags, content='place_images', content_rowid='id');
CREATE VIRTUAL TABLE IF NOT EXISTS refs_fts USING fts5(text, notes, content='place_references', content_rowid='id');
-- places_fts sync
CREATE TRIGGER IF NOT EXISTS places_ai AFTER INSERT ON places BEGIN
  INSERT INTO places_fts(rowid, name, my_notes, location)
  VALUES (new.id, new.name, new.my_notes, new.location);
END;
CREATE TRIGGER IF NOT EXISTS places_ad AFTER DELETE ON places BEGIN
  INSERT INTO places_fts(places_fts, rowid, name, my_notes, location)
  VALUES ('delete', old.id, old.name, old.my_notes, old.location);
END;
CREATE TRIGGER IF NOT EXISTS places_au AFTER UPDATE ON places BEGIN
  INSERT INTO places_fts(places_fts, rowid, name, my_notes, location)
  VALUES ('delete', old.id, old.name, old.my_notes, old.location);
  INSERT INTO places_fts(rowid, name, my_notes, location)
  VALUES (new.id, new.name, new.my_notes, new.location);
END;
-- images_fts sync
CREATE TRIGGER IF NOT EXISTS place_images_ai AFTER INSERT ON place_images BEGIN
  INSERT INTO images_fts(rowid, caption, tags)
  VALUES (new.id, new.caption, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS place_images_ad AFTER DELETE ON place_images BEGIN
  INSERT INTO images_fts(images_fts, rowid, caption, tags)
  VALUES ('delete', old.id, old.caption, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS place_images_au AFTER UPDATE ON place_images BEGIN
  INSERT INTO images_fts(images_fts, rowid, caption, tags)
  VALUES ('delete', old.id, old.caption, old.tags);
  INSERT INTO images_fts(rowid, caption, tags)
  VALUES (new.id, new.caption, new.tags);
END;
-- refs_fts sync
CREATE TRIGGER IF NOT EXISTS place_references_ai AFTER INSERT ON place_references BEGIN
  INSERT INTO refs_fts(rowid, text, notes)
  VALUES (new.id, new.text, new.notes);
END;
CREATE TRIGGER IF NOT EXISTS place_references_ad AFTER DELETE ON place_references BEGIN
  INSERT INTO refs_fts(refs_fts, rowid, text, notes)
  VALUES ('delete', old.id, old.text, old.notes);
END;
CREATE TRIGGER IF NOT EXISTS place_references_au AFTER UPDATE ON place_references BEGIN
  INSERT INTO refs_fts(refs_fts, rowid, text, notes)
  VALUES ('delete', old.id, old.text, old.notes);
  INSERT INTO refs_fts(rowid, text, notes)
  VALUES (new.id, new.text, new.notes);
END;
"""


async def connect(path: str | Path) -> aiosqlite.Connection:
    """Open a channel-KB connection with WAL mode and FK enforcement."""
    conn = await aiosqlite.connect(str(path), detect_types=sqlite3.PARSE_DECLTYPES)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = aiosqlite.Row
    return conn


async def init_db(conn: aiosqlite.Connection) -> None:
    """Create all channel-KB tables. Idempotent: safe to re-run against an
    already-initialized DB (every CREATE uses IF NOT EXISTS) so the daemon
    survives restarts."""
    await conn.executescript(SCHEMA)
    await conn.commit()
