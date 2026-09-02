"""Migration 8: memory_extractions no longer FK-constrains session_id.

BPC keys evaluator runs by a synthetic window id (user:working_dir:window),
which is not a row in `sessions`. Before migration 8 the FK rejected every
audit insert with "FOREIGN KEY constraint failed".
"""


async def test_insert_with_non_session_id_succeeds(tmp_path):
    from src.storage.database import DatabaseManager

    db = DatabaseManager(f"sqlite:///{tmp_path / 'm.db'}")
    await db.initialize()
    try:
        async with db.get_connection() as conn:
            # a real user must exist (user_id FK is retained)
            await conn.execute(
                "INSERT INTO users (user_id, telegram_username) VALUES (?, ?)",
                (123, "tester"),
            )
            # synthetic window session_id that is NOT in `sessions`
            await conn.execute(
                """INSERT INTO memory_extractions
                   (session_id, user_id, prompt_hash, validation_status)
                   VALUES (?, ?, ?, ?)""",
                ("123:/home/u/channel-workspace:4", 123, "deadbeef", "success"),
            )
            await conn.commit()
            cur = await conn.execute(
                "SELECT session_id FROM memory_extractions WHERE user_id = 123"
            )
            row = await cur.fetchone()
            assert row[0] == "123:/home/u/channel-workspace:4"
    finally:
        await db.close()
