"""Alembic env for llm_analytics.db -- shared body in
src/storage/schema/alembic_env.py."""

from alembic import context

from src.storage.schema.alembic_env import run_env

run_env(context, "llm_analytics")
