import asyncio

import asyncpg


async def main() -> None:
    user = "postgres"
    password = "1234"
    ports = [5432, 5433, 5434, 5440, 5431]
    for port in ports:
        dsn = f"postgresql://{user}:{password}@localhost:{port}/postgres"
        try:
            conn = await asyncpg.connect(dsn, timeout=3)
            await conn.close()
            print(f"PORT_OK {port}")
        except Exception as e:
            msg = str(e).splitlines()[0] if str(e) else ""
            print(f"PORT_FAIL {port} {type(e).__name__} {msg}")


if __name__ == "__main__":
    asyncio.run(main())

