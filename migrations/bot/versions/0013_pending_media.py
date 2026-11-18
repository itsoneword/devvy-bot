"""pending_media: media messages kept until their file is downloaded

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-11

dv-c977: every voice / photo / document message writes a row at receipt
(file_id + the raw update JSON) before the first getFile. A file Telegram does
not hand out within the inline window is retried in the background, also
after a restart, and the row is closed (done / stopped / failed) when its turn
has run. ``failed`` rows keep their file_id so they can be retried by a tap.
Times are epoch seconds (UTC).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, Sequence[str], None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_media",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("chat_id", sa.INTEGER(), nullable=False),
        sa.Column("thread_id", sa.INTEGER(), nullable=True),
        sa.Column("message_id", sa.INTEGER(), nullable=False),
        sa.Column("user_id", sa.INTEGER(), nullable=False),
        sa.Column("profile_slug", sa.TEXT(), nullable=True),
        sa.Column("kind", sa.TEXT(), nullable=False),
        sa.Column("file_id", sa.TEXT(), nullable=False),
        sa.Column("update_json", sa.TEXT(), nullable=False),
        sa.Column(
            "status", sa.TEXT(), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column(
            "attempts", sa.INTEGER(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("last_error", sa.TEXT(), nullable=True),
        sa.Column("status_message_id", sa.INTEGER(), nullable=True),
        sa.Column("sent_at", sa.REAL(), nullable=True),
        sa.Column("received_at", sa.REAL(), nullable=False),
        sa.Column("window_start", sa.REAL(), nullable=True),
        sa.Column("closed_at", sa.REAL(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_pending_media_status", "pending_media", ["status"])


def downgrade() -> None:
    op.drop_index("idx_pending_media_status", table_name="pending_media")
    op.drop_table("pending_media")
