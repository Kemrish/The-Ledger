import asyncio
import os

import asyncpg
from dotenv import load_dotenv


async def main() -> None:
    load_dotenv()
    dsn = os.environ.get("LEDGER_TEST_DSN", "").strip()
    if not dsn:
        raise RuntimeError("LEDGER_TEST_DSN missing")

    conn = await asyncpg.connect(dsn)
    rows = await conn.fetch(
        "SELECT projection_name, last_position, updated_at FROM projection_checkpoints ORDER BY projection_name"
    )
    print("projection_checkpoints:", [
        {
            "projection_name": r["projection_name"],
            "last_position": r["last_position"],
            "updated_at": str(r["updated_at"]),
        }
        for r in rows
    ])

    max_row = await conn.fetchrow(
        "SELECT COALESCE(MAX(global_position), 0) AS max_pos, MAX(recorded_at) AS max_time FROM events"
    )
    print("events:", {"max_pos": max_row["max_pos"], "max_time": str(max_row["max_time"])})

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

