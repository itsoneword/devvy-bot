# Database migrations

The bot keeps four SQLite files. Each has its own schema module, its own
Alembic script directory and its own `alembic_version` table:

| DB | Schema (SQLAlchemy Core tables) | Migrations | Opened by |
|---|---|---|---|
| `bot.db` | `src/storage/schema/bot.py` | `migrations/bot/` | `DatabaseManager` |
| `data.db` | `src/storage/schema/data.py` | `migrations/data/` | `UserDataManager` |
| `llm_analytics.db` | `src/llm_analytics/schema.py` | `migrations/llm_analytics/` | `llm_analytics.db.init_db` |
| `channel_intel.db` | `src/channel_intel/schema.py` | `migrations/channel_intel/` | `channel_intel.db.init_db` |

Queries are raw SQL. The schema modules exist for Alembic autogenerate and
for diffing. No query code imports them.

## What happens at startup

Every process calls `ensure_schema(db, path)` (`src/storage/schema/migrate.py`)
before it uses a DB. It takes `BEGIN IMMEDIATE`, so if two processes start
together the second one waits and then finds nothing left to do. It then
does one of these:

- **empty file**: `upgrade head`.
- **at head**: nothing. No backup is written.
- **behind head**: takes a backup, then runs `upgrade head`.
- **pre-Alembic install** (has `schema_version`, or bare tables): takes a
  backup and replays the frozen legacy history (`src/storage/schema/legacy.py`).
  It then checks that the result is structurally equal to the baseline
  revision, stamps the baseline, and runs `upgrade head`. If the check fails,
  it refuses to start and leaves the file untouched.

All of these steps run in one transaction. If anything fails, the file is
rolled back.

**Backups** are written with the sqlite3 backup API, which is safe under WAL,
to `<db dir>/backups/<db>.<utc>.pre-<revision>.db`. The last 3 per DB are
kept.

## Changing the schema

1. Edit the table in the DB's schema module. Keep these rules:
   - New columns go **at the end**, because many reads use `SELECT *` or `Model(*row)`.
   - Use the uppercase SQLAlchemy types (`TIMESTAMP`, `DATETIME`, `DATE`,
     `BOOLEAN`, `JSON`, `INTEGER`, `TEXT`, `REAL`). The `detect_types`
     converters key on the declared type name.
   - Spell defaults as `server_default=text("...")`. Set `nullable` explicitly.
2. Build a scratch DB at head and generate the revision against it:
   ```bash
   poetry run alembic -n bot -x db=/tmp/bot.db upgrade head
   poetry run alembic -n bot -x db=/tmp/bot.db revision --autogenerate -m "add foo"
   ```
   Without `-x db=...`, Alembic uses your configured data root, which is your
   real DB.
3. Review the generated file by hand. Autogenerate does not see CHECK changes,
   FTS tables, triggers or views (see below).
4. Test it:
   ```bash
   poetry run alembic -n bot -x db=/tmp/bot.db upgrade head
   poetry run alembic -n bot -x db=/tmp/bot.db check      # model == DB
   poetry run python scripts/schema_diff.py /tmp/bot.db fresh --db bot
   poetry run pytest tests/unit/test_storage
   ```

`scripts/schema_diff.py` compares any two of: a DB file, a snapshot JSON,
`fresh` (built by Alembic) or `legacy` (the pre-Alembic replay). Use
`--dir <copies>` to check all four DBs at once.

## FTS5, triggers and views

Alembic does not manage these objects:

- the FTS5 tables (`messages_fts`, `memory_facts_fts`, `places_fts`,
  `images_fts`, `refs_fts`) and their shadow tables
- the 15 sync triggers (6 in `bot`, 9 in `channel_intel`)
- the `daily_stats` and `user_stats` views

They are created by raw-SQL revisions (`0002_baseline_fts_triggers_views.py`),
and `include_object` hides them from autogenerate.

SQLite's `ALTER TABLE` can only add columns. Any other change uses
`batch_alter_table`, which builds a new table, copies the rows, drops the old
table and renames. That has two consequences:

- **Rebuilding a table with FTS triggers** (`messages`, `memory_facts`,
  `places`, `place_images`, `place_references`) **drops its triggers
  silently.** The revision must recreate them. Copy the SQL from the `0002`
  revision.
- **Rebuilding a table that a view reads** (`messages`, `users`, `sessions`)
  **fails** with `error in view daily_stats`. Drop the views first and
  recreate them afterwards, in the same revision.

`tests/unit/test_storage/test_alembic_schema.py::test_fts_trigger_guard`
fails if head is missing any trigger. Plain `op.add_column` is a real
`ALTER TABLE` and leaves triggers alone.

## Rollback

- **Code only, no schema change yet:** `git revert`, then restart. Stamping
  only adds `alembic_version`. `schema_version` stays frozen at bot 26 / data 4,
  so the old code finds nothing to migrate.
- **After a revision:**
  1. Stop the bot.
  2. Either restore the backup (copy `backups/<db>.<utc>.pre-<rev>.db` over
     the DB and delete its `-wal` and `-shm` files), or downgrade in place
     with `alembic -n <db> -x db=<path> downgrade -1`. The downgrade only
     works if the revision has a correct `downgrade()`.
  3. Revert the code and start the bot.

## Legacy bridge

`legacy.py` and the bridge branch in `migrate.py` exist to upgrade installs
that predate Alembic. They stay for one release and are then deleted. Never
edit them. Schema changes always go in a new revision.
