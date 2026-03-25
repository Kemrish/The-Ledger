from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv


async def main() -> None:
    load_dotenv()

    dsn = os.environ.get("LEDGER_TEST_DSN")
    if not dsn:
        raise RuntimeError("LEDGER_TEST_DSN not set (check .env).")

    # Example: postgresql://user:pass@host:5432/ledger_test
    admin_db = dsn.rsplit("/", 1)[0] + "/postgres"
    target_db = dsn.rsplit("/", 1)[1]

    schema_path = Path(__file__).resolve().parents[1] / "src" / "schema.sql"
    schema_sql = schema_path.read_text(encoding="utf-8")

    # 1) Create database (idempotent).
    admin_conn = await asyncpg.connect(admin_db)
    try:
        await admin_conn.execute(f'CREATE DATABASE "{target_db}"')
        print(f"Created database: {target_db}")
    except asyncpg.exceptions.DuplicateDatabaseError:
        print(f"Database already exists: {target_db}")
    finally:
        await admin_conn.close()

    # 2) Apply schema (idempotent due to IF NOT EXISTS statements).
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
        await conn.execute(schema_sql)
        print("Applied schema.sql successfully.")

        rows = await conn.fetch(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        )
        print("Public tables:")
        for r in rows:
            print(f"- {r['table_name']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

