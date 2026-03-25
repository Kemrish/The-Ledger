import asyncio
import os

import asyncpg
from dotenv import load_dotenv


async def main() -> None:
    load_dotenv()
    dsn = os.environ.get("LEDGER_TEST_DSN")
    print("DSN_repr", repr(dsn))
    if not dsn:
        raise RuntimeError("LEDGER_TEST_DSN missing")
    dsn = dsn.strip()
    try:
        conn = await asyncpg.connect(dsn)
        print("CONNECT_OK")
        await conn.close()
    except Exception as e:
        print("CONNECT_FAIL", type(e).__name__, str(e))


if __name__ == "__main__":
    asyncio.run(main())

