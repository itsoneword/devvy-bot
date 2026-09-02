import pytest_asyncio

from src.channel_intel.db import connect, init_db


@pytest_asyncio.fixture
async def db():
    conn = await connect(":memory:")
    await init_db(conn)
    yield conn
    await conn.close()
