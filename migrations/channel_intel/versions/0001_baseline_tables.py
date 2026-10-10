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
        "kb_users",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("owner", sa.TEXT(), server_default=sa.text("'me'"), nullable=False),
        sa.Column("tg_user_id", sa.INTEGER(), nullable=True),
        sa.Column("username", sa.TEXT(), nullable=True),
        sa.Column("display_name", sa.TEXT(), nullable=True),
        sa.Column("first_seen", sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "place_references",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("owner", sa.TEXT(), server_default=sa.text("'me'"), nullable=False),
        sa.Column("source_channel", sa.TEXT(), nullable=True),
        sa.Column("text", sa.TEXT(), nullable=True),
        sa.Column("image_refs", sa.TEXT(), nullable=True),
        sa.Column("tags", sa.TEXT(), nullable=True),
        sa.Column("notes", sa.TEXT(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "places",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("owner", sa.TEXT(), server_default=sa.text("'me'"), nullable=False),
        sa.Column("name", sa.TEXT(), nullable=False),
        sa.Column("type", sa.TEXT(), nullable=True),
        sa.Column("location", sa.TEXT(), nullable=True),
        sa.Column("lat", sa.REAL(), nullable=True),
        sa.Column("lon", sa.REAL(), nullable=True),
        sa.Column("travel_time_from_home", sa.TEXT(), nullable=True),
        sa.Column("price", sa.TEXT(), nullable=True),
        sa.Column("opening_hours", sa.TEXT(), nullable=True),
        sa.Column("features", sa.TEXT(), nullable=True),
        sa.Column(
            "status", sa.TEXT(), server_default=sa.text("'idea'"), nullable=False
        ),
        sa.Column("liked", sa.INTEGER(), nullable=True),
        sa.Column("my_notes", sa.TEXT(), nullable=True),
        sa.Column("source", sa.TEXT(), nullable=True),
        sa.Column("enriched_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "posts",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("owner", sa.TEXT(), server_default=sa.text("'me'"), nullable=False),
        sa.Column("place_ids", sa.TEXT(), nullable=True),
        sa.Column("body", sa.TEXT(), nullable=True),
        sa.Column("selected_image_refs", sa.TEXT(), nullable=True),
        sa.Column(
            "status", sa.TEXT(), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "preferences",
        sa.Column("owner", sa.TEXT(), nullable=True),
        sa.Column("liked_features", sa.TEXT(), nullable=True),
        sa.Column("liked_place_ids", sa.TEXT(), nullable=True),
        sa.Column("budget_norms", sa.TEXT(), nullable=True),
        sa.Column("home_location", sa.TEXT(), nullable=True),
        sa.Column("default_radius", sa.TEXT(), nullable=True),
        sa.PrimaryKeyConstraint("owner"),
    )
    op.create_table(
        "comments",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("owner", sa.TEXT(), server_default=sa.text("'me'"), nullable=False),
        sa.Column("user_id", sa.INTEGER(), nullable=True),
        sa.Column("post_ref", sa.TEXT(), nullable=True),
        sa.Column("text", sa.TEXT(), nullable=True),
        sa.Column("topics", sa.TEXT(), nullable=True),
        sa.Column("sentiment", sa.TEXT(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["kb_users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "place_images",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("owner", sa.TEXT(), server_default=sa.text("'me'"), nullable=False),
        sa.Column("place_id", sa.INTEGER(), nullable=True),
        sa.Column("file_path", sa.TEXT(), nullable=False),
        sa.Column("caption", sa.TEXT(), nullable=True),
        sa.Column("tags", sa.TEXT(), nullable=True),
        sa.Column("vision_model", sa.TEXT(), nullable=True),
        sa.Column(
            "confirmed", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.ForeignKeyConstraint(
            ["place_id"],
            ["places.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "reference_places",
        sa.Column("reference_id", sa.INTEGER(), nullable=False),
        sa.Column("place_id", sa.INTEGER(), nullable=False),
        sa.ForeignKeyConstraint(
            ["place_id"],
            ["places.id"],
        ),
        sa.ForeignKeyConstraint(
            ["reference_id"],
            ["place_references.id"],
        ),
        sa.PrimaryKeyConstraint("reference_id", "place_id"),
    )
    op.create_table(
        "visits",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("owner", sa.TEXT(), server_default=sa.text("'me'"), nullable=False),
        sa.Column("place_id", sa.INTEGER(), nullable=False),
        sa.Column("date", sa.TEXT(), nullable=True),
        sa.Column("activities", sa.TEXT(), nullable=True),
        sa.Column("costs", sa.TEXT(), nullable=True),
        sa.Column("narrative", sa.TEXT(), nullable=True),
        sa.Column("photo_refs", sa.TEXT(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.ForeignKeyConstraint(
            ["place_id"],
            ["places.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table("visits")
    op.drop_table("reference_places")
    op.drop_table("place_images")
    op.drop_table("comments")
    op.drop_table("preferences")
    op.drop_table("posts")
    op.drop_table("places")
    op.drop_table("place_references")
    op.drop_table("kb_users")
