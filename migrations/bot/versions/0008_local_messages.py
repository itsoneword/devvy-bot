"""local_messages: the local channel's inbox

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-31

dv-fe4d: a local install (``CHANNEL=local``, no Telegram) delivers what the
bot would have sent into this table instead; the TUI reads it over
``/internal/inbox``. ``id`` is the message id the pin store and the ledger key
on, ``buttons_json`` an owner prompt's ``[[label, callback_data], ...]``.
``seq`` is the change cursor the TUI polls with: bumped on every insert,
edit and pin change, so an edited card (an ask that timed out or was
answered) reaches a client whose cursor is past its ``id``.
Empty on a Telegram install.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "local_messages",
        sa.Column("id", sa.INTEGER(), nullable=True),
        sa.Column("chat_id", sa.INTEGER(), nullable=False),
        sa.Column("text", sa.TEXT(), nullable=False),
        sa.Column("parse_mode", sa.TEXT(), nullable=True),
        sa.Column("buttons_json", sa.TEXT(), nullable=True),
        sa.Column("pinned", sa.INTEGER(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("edited_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("read_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("seq", sa.INTEGER(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_local_messages_pinned", "local_messages", ["pinned", "chat_id"]
    )
    op.create_index("idx_local_messages_seq", "local_messages", ["seq"])


def downgrade() -> None:
    op.drop_index("idx_local_messages_seq", table_name="local_messages")
    op.drop_index("idx_local_messages_pinned", table_name="local_messages")
    op.drop_table("local_messages")
