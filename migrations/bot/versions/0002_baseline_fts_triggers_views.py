"""baseline FTS5, triggers, views (raw SQL)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10

Objects Alembic cannot model: messages_fts + memory_facts_fts (FTS5), their 6 sync triggers, and the
daily_stats / user_stats views.
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
    """CREATE VIRTUAL TABLE memory_facts_fts
                    USING fts5(content, content='memory_facts', content_rowid='id')""",
    """CREATE VIRTUAL TABLE messages_fts USING fts5(
                    prompt, response,
                    content='messages', content_rowid='message_id',
                    prefix='2 3 4'
                )""",
]

TRIGGERS = [
    """CREATE TRIGGER memory_facts_ad
                AFTER DELETE ON memory_facts BEGIN
                    INSERT INTO memory_facts_fts(memory_facts_fts, rowid, content)
                    VALUES ('delete', old.id, old.content);
                END""",
    """CREATE TRIGGER memory_facts_ai
                AFTER INSERT ON memory_facts BEGIN
                    INSERT INTO memory_facts_fts(rowid, content)
                    VALUES (new.id, new.content);
                END""",
    """CREATE TRIGGER memory_facts_au
                AFTER UPDATE ON memory_facts BEGIN
                    INSERT INTO memory_facts_fts(memory_facts_fts, rowid, content)
                    VALUES ('delete', old.id, old.content);
                    INSERT INTO memory_facts_fts(rowid, content)
                    VALUES (new.id, new.content);
                END""",
    """CREATE TRIGGER messages_fts_ad
                AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, prompt, response)
                    VALUES ('delete', old.message_id, old.prompt, COALESCE(old.response, ''));
                END""",
    """CREATE TRIGGER messages_fts_ai
                AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, prompt, response)
                    VALUES (new.message_id, new.prompt, COALESCE(new.response, ''));
                END""",
    """CREATE TRIGGER messages_fts_au
                AFTER UPDATE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, prompt, response)
                    VALUES ('delete', old.message_id, old.prompt, COALESCE(old.response, ''));
                    INSERT INTO messages_fts(rowid, prompt, response)
                    VALUES (new.message_id, new.prompt, COALESCE(new.response, ''));
                END""",
]

VIEWS = [
    """CREATE VIEW daily_stats AS
                SELECT
                    date(timestamp) as date,
                    COUNT(DISTINCT user_id) as active_users,
                    COUNT(*) as total_messages,
                    SUM(cost) as total_cost,
                    AVG(duration_ms) as avg_duration
                FROM messages
                GROUP BY date(timestamp)""",
    """CREATE VIEW user_stats AS
                SELECT
                    u.user_id,
                    u.telegram_username,
                    COUNT(DISTINCT s.session_id) as total_sessions,
                    COUNT(m.message_id) as total_messages,
                    SUM(m.cost) as total_cost,
                    MAX(m.timestamp) as last_activity
                FROM users u
                LEFT JOIN sessions s ON u.user_id = s.user_id
                LEFT JOIN messages m ON u.user_id = m.user_id
                GROUP BY u.user_id""",
]


def upgrade() -> None:
    for sql in FTS_TABLES + TRIGGERS + VIEWS:
        op.execute(sql)


_VIEW_NAMES = ["daily_stats", "user_stats"]
_TRIGGER_NAMES = [
    "memory_facts_ad",
    "memory_facts_ai",
    "memory_facts_au",
    "messages_fts_ad",
    "messages_fts_ai",
    "messages_fts_au",
]
_FTS_NAMES = ["memory_facts_fts", "messages_fts"]


def downgrade() -> None:
    for view in _VIEW_NAMES:
        op.execute(f"DROP VIEW IF EXISTS {view}")
    for trigger in _TRIGGER_NAMES:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in _FTS_NAMES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
