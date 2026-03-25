"""Pytest fixtures: in-memory or real Postgres for tests."""
import asyncio
import os

import asyncpg
import pytest

from src.event_store import EventStore


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def get_pg_dsn() -> str:
    return os.environ.get(
        "LEDGER_TEST_DSN",
        "postgresql://postgres:postgres@localhost:5432/ledger_test",
    )


@pytest.fixture
async def pg_pool():
    dsn = get_pg_dsn()
    pool = None
    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, command_timeout=10)
        yield pool
    finally:
        if pool is not None:
            await pool.close()


@pytest.fixture
async def db_conn(pg_pool):
    conn = await pg_pool.acquire()
    try:
        yield conn
    finally:
        await pg_pool.release(conn)


@pytest.fixture
async def store(db_conn):
    return EventStore(db_conn)


@pytest.fixture
async def migrated_db(db_conn):
    """Apply schema to the test database."""
    schema_path = os.path.join(os.path.dirname(__file__), "..", "src", "schema.sql")
    with open(schema_path) as f:
        await db_conn.execute(f.read())
    yield db_conn
