"""baseline tables

Revision ID: 0001
Revises:
Create Date: 2026-10-10

Review before committing (docs/database-migrations.md):
- Column order / declared type names must match the model exactly.
- A batch_alter_table on a table with FTS5 sync triggers (bot: messages,
  memory_facts; channel_intel: places, place_images, place_references)
  rebuilds it and DROPS its triggers -- recreate them in this revision.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "docs",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("kind", sa.TEXT(), nullable=False),
        sa.Column("period", sa.TEXT(), nullable=True),
        sa.Column("title", sa.TEXT(), nullable=True),
        sa.Column("content", sa.TEXT(), nullable=False),
        sa.Column("imported_from", sa.TEXT(), nullable=True),
        sa.Column("created_at", sa.TEXT(), nullable=False),
        sa.Column("updated_at", sa.TEXT(), nullable=False),
        sa.CheckConstraint("kind IN ('note','program')"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_docs_user_kind", "docs", ["user_id", "kind", "period"], unique=False
    )

    op.create_table(
        "issues",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("title", sa.TEXT(), nullable=False),
        sa.Column("status", sa.TEXT(), nullable=False),
        sa.Column("started_on", sa.TEXT(), nullable=True),
        sa.Column("resolved_on", sa.TEXT(), nullable=True),
        sa.Column("symptom", sa.TEXT(), nullable=True),
        sa.Column("restrictions", sa.TEXT(), nullable=True),
        sa.Column("escalation", sa.TEXT(), nullable=True),
        sa.Column("notes", sa.TEXT(), nullable=True),
        sa.Column("imported_from", sa.TEXT(), nullable=True),
        sa.Column("created_at", sa.TEXT(), nullable=False),
        sa.Column("updated_at", sa.TEXT(), nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE','RESOLVED','CHRONIC')"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_issues_user_status", "issues", ["user_id", "status"], unique=False
    )

    op.create_table(
        "logs_activity",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("date", sa.TEXT(), nullable=False),
        sa.Column("type", sa.TEXT(), nullable=False),
        sa.Column("duration_min", sa.REAL(), nullable=True),
        sa.Column("intensity", sa.TEXT(), nullable=True),
        sa.Column("source", sa.TEXT(), nullable=False),
        sa.Column("imported_from", sa.TEXT(), nullable=True),
        sa.Column("created_at", sa.TEXT(), nullable=False),
        sa.Column("updated_at", sa.TEXT(), nullable=False),
        sa.Column("kcal_active", sa.REAL(), nullable=True),
        sa.Column("kcal_total", sa.REAL(), nullable=True),
        sa.Column("exercise_min", sa.REAL(), nullable=True),
        sa.Column("stand_h", sa.REAL(), nullable=True),
        sa.Column("steps", sa.REAL(), nullable=True),
        sa.Column("distance_km", sa.REAL(), nullable=True),
        sa.CheckConstraint("source IN ('user_reported','measured','estimated')"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_logs_activity_user_date",
        "logs_activity",
        ["user_id", "date"],
        unique=False,
    )

    op.create_table(
        "logs_food",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("date", sa.TEXT(), nullable=False),
        sa.Column("meal", sa.TEXT(), nullable=False),
        sa.Column("time", sa.TEXT(), nullable=True),
        sa.Column("kcal", sa.REAL(), nullable=True),
        sa.Column("protein", sa.REAL(), nullable=True),
        sa.Column("fat", sa.REAL(), nullable=True),
        sa.Column("carb", sa.REAL(), nullable=True),
        sa.Column("confidence", sa.TEXT(), nullable=True),
        sa.Column("source", sa.TEXT(), nullable=True),
        sa.Column("imported_from", sa.TEXT(), nullable=True),
        sa.Column("created_at", sa.TEXT(), nullable=False),
        sa.Column("updated_at", sa.TEXT(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_logs_food_user_date", "logs_food", ["user_id", "date"], unique=False
    )

    op.create_table(
        "logs_sleep",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("date", sa.TEXT(), nullable=False),
        sa.Column("bed", sa.TEXT(), nullable=True),
        sa.Column("wake", sa.TEXT(), nullable=True),
        sa.Column("sleep_min", sa.REAL(), nullable=True),
        sa.Column("onset_min", sa.REAL(), nullable=True),
        sa.Column("score", sa.REAL(), nullable=True),
        sa.Column("hrv", sa.REAL(), nullable=True),
        sa.Column("rhr", sa.REAL(), nullable=True),
        sa.Column("weight_kg", sa.REAL(), nullable=True),
        sa.Column("wellbeing", sa.TEXT(), nullable=True),
        sa.Column("alcohol_u", sa.REAL(), nullable=True),
        sa.Column("thc", sa.TEXT(), nullable=True),
        sa.Column("room_temp", sa.REAL(), nullable=True),
        sa.Column("out_night", sa.REAL(), nullable=True),
        sa.Column("out_day", sa.REAL(), nullable=True),
        sa.Column("imported_from", sa.TEXT(), nullable=True),
        sa.Column("created_at", sa.TEXT(), nullable=False),
        sa.Column("updated_at", sa.TEXT(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "date"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "logs_train",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("date", sa.TEXT(), nullable=False),
        sa.Column("session", sa.TEXT(), nullable=True),
        sa.Column("exercise", sa.TEXT(), nullable=False),
        sa.Column("set_n", sa.TEXT(), nullable=True),
        sa.Column("weight_kg", sa.TEXT(), nullable=True),
        sa.Column("reps", sa.TEXT(), nullable=True),
        sa.Column("rpe", sa.REAL(), nullable=True),
        sa.Column("imported_from", sa.TEXT(), nullable=True),
        sa.Column("created_at", sa.TEXT(), nullable=False),
        sa.Column("updated_at", sa.TEXT(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_logs_train_user_date", "logs_train", ["user_id", "date"], unique=False
    )
    op.create_index(
        "idx_logs_train_user_exercise",
        "logs_train",
        ["user_id", "exercise"],
        unique=False,
    )

    op.create_table(
        "portions",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("phrase", sa.TEXT(), nullable=False),
        sa.Column("grams", sa.REAL(), nullable=False),
        sa.Column("learned_from", sa.TEXT(), nullable=True),
        sa.Column("updated_on", sa.TEXT(), nullable=True),
        sa.Column("created_at", sa.TEXT(), nullable=False),
        sa.Column("updated_at", sa.TEXT(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "phrase"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "products",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("name", sa.TEXT(), nullable=False),
        sa.Column("brand", sa.TEXT(), server_default=sa.text("('')"), nullable=False),
        sa.Column("kind", sa.TEXT(), nullable=False),
        sa.Column("kcal_100g", sa.REAL(), nullable=True),
        sa.Column("protein", sa.REAL(), nullable=True),
        sa.Column("fat", sa.REAL(), nullable=True),
        sa.Column("carb", sa.REAL(), nullable=True),
        sa.Column("salt", sa.REAL(), nullable=True),
        sa.Column("provenance", sa.TEXT(), nullable=False),
        sa.Column("confirmed_on", sa.TEXT(), nullable=True),
        sa.Column("created_by", sa.INTEGER(), nullable=True),
        sa.Column("created_at", sa.TEXT(), nullable=False),
        sa.Column("updated_at", sa.TEXT(), nullable=False),
        sa.CheckConstraint("kind IN ('product','dish')"),
        sa.CheckConstraint("provenance IN ('label_photo','official','estimate')"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "brand"),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table("products")
    op.drop_table("portions")
    op.drop_index("idx_logs_train_user_exercise", table_name="logs_train")
    op.drop_index("idx_logs_train_user_date", table_name="logs_train")

    op.drop_table("logs_train")
    op.drop_table("logs_sleep")
    op.drop_index("idx_logs_food_user_date", table_name="logs_food")

    op.drop_table("logs_food")
    op.drop_index("idx_logs_activity_user_date", table_name="logs_activity")

    op.drop_table("logs_activity")
    op.drop_index("idx_issues_user_status", table_name="issues")

    op.drop_table("issues")
    op.drop_index("idx_docs_user_kind", table_name="docs")

    op.drop_table("docs")
