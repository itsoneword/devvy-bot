"""channel_posts: the devlog channel's drafts and published posts

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-12

dv-fc9d: one row per draft the ``devlog`` agent files. A row's content
(text, headline, image sha256) never changes: an edit files a new row and
marks the old one ``superseded``, so a preview's publish button is bound to
exactly what the owner saw. ``status``: draft | blocked (leak hits) |
publishing | published | cancelled | superseded.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, Sequence[str], None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "channel_posts",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("profile", sa.TEXT(), nullable=False),
        sa.Column("status", sa.TEXT(), nullable=False),
        sa.Column("text", sa.TEXT(), nullable=False),
        sa.Column("headline", sa.TEXT(), nullable=True),
        sa.Column("image_path", sa.TEXT(), nullable=False),
        sa.Column("image_sha256", sa.TEXT(), nullable=False),
        sa.Column("image_kind", sa.TEXT(), nullable=False),
        sa.Column(
            "leak_hits", sa.TEXT(), server_default=sa.text("'[]'"), nullable=False
        ),
        sa.Column("superseded_by", sa.INTEGER(), nullable=True),
        sa.Column("preview_chat_id", sa.INTEGER(), nullable=True),
        sa.Column("preview_message_id", sa.INTEGER(), nullable=True),
        sa.Column("channel_message_id", sa.INTEGER(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("published_at", sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_channel_posts_profile", "channel_posts", ["profile", "status"])


def downgrade() -> None:
    op.drop_index("idx_channel_posts_profile", table_name="channel_posts")
    op.drop_table("channel_posts")
