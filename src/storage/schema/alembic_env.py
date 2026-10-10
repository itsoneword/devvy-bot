"""Shared Alembic ``env.py`` body for the 4 SQLite DBs (dv-5e4f Phase 1).

Each ``migrations/<db>/env.py`` is two lines that call :func:`run_env`.
One ``alembic.ini`` holds a named section per DB (``alembic -n bot ...``);
every DB has its own script directory and its own ``alembic_version`` table,
because the DBs live in different processes and migrate independently.

Two ways in:

- **The app** (``src.storage.schema.migrate``) passes an open SQLAlchemy
  connection via ``config.attributes["connection"]`` -- it already holds the
  ``BEGIN IMMEDIATE`` lock and owns the transaction; env.py must not touch
  logging config or commit.
- **The CLI** (``alembic -n <db> current|check|revision --autogenerate``)
  gets no connection; the DB path is ``-x db=<path>``, else the configured
  data layout's path for that DB.
"""

from __future__ import annotations

import importlib
from logging.config import fileConfig
from typing import Any, Optional

from sqlalchemy import MetaData, create_engine
from sqlalchemy.pool import NullPool

# db name -> module holding its ``metadata``.
METADATA_MODULES = {
    "bot": "src.storage.schema.bot",
    "data": "src.storage.schema.data",
    "llm_analytics": "src.llm_analytics.schema",
    "channel_intel": "src.channel_intel.schema",
}

# Objects Alembic must neither create nor diff: version bookkeeping, SQLite
# internals, and FTS5 virtual tables + their shadow tables (raw-SQL
# revisions own those, together with their triggers and the views).
_UNMANAGED_TABLES = frozenset({"schema_version", "alembic_version"})


def is_unmanaged_table(name: Optional[str]) -> bool:
    if not name:
        return False
    return (
        name in _UNMANAGED_TABLES
        or name.startswith("sqlite_")
        or name.endswith("_fts")
        or "_fts_" in name
    )


def include_object(
    obj: Any, name: Optional[str], type_: str, reflected: bool, compare_to: Any
) -> bool:
    if type_ == "table":
        return not is_unmanaged_table(name)
    table = getattr(obj, "table", None)
    if table is not None and is_unmanaged_table(table.name):
        return False
    return True


def target_metadata(db_name: str) -> MetaData:
    metadata: MetaData = importlib.import_module(METADATA_MODULES[db_name]).metadata
    return metadata


def default_db_path(db_name: str) -> str:
    """CLI fallback: the configured data layout (``DEVVY_DATA_ROOT``)."""
    from src.utils.paths import data_layout

    layout = data_layout()
    return str(
        {
            "bot": layout.bot_db,
            "data": layout.data_db,
            "llm_analytics": layout.llm_analytics_db,
            "channel_intel": layout.channel_intel_db,
        }[db_name]
    )


def _configure(context: Any, db_name: str, **kw: Any) -> None:
    context.configure(
        target_metadata=target_metadata(db_name),
        include_object=include_object,
        render_as_batch=True,  # SQLite ALTER is limited; batch = copy+swap
        compare_type=True,
        **kw,
    )


def run_env(context: Any, db_name: str) -> None:
    config = context.config
    connection = config.attributes.get("connection")

    if connection is not None:
        # In-process (ensure_schema): caller holds the lock + transaction.
        _configure(context, db_name, connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    if config.config_file_name:
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    path = context.get_x_argument(as_dictionary=True).get("db") or default_db_path(
        db_name
    )
    url = f"sqlite:///{path}"

    if context.is_offline_mode():
        _configure(context, db_name, url=url, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(url, poolclass=NullPool)
    with engine.connect() as conn:
        _configure(context, db_name, connection=conn)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
