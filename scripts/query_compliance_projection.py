"""
Print compliance_audit_projection rows for an application (no psql required).

Usage (from repo root):
  uv run python scripts/query_compliance_projection.py APEX-0007
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import asyncpg
from dotenv import load_dotenv


async def main() -> None:
    load_dotenv()
    if len(sys.argv) < 2:
        print("Usage: uv run python scripts/query_compliance_projection.py <application_id>")
        raise SystemExit(2)
    app_id = sys.argv[1].strip()
    dsn = os.environ.get("LEDGER_TEST_DSN", "").strip()
    if not dsn:
        raise RuntimeError("Set LEDGER_TEST_DSN in .env or the environment.")

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT application_id, as_of_recorded_at, status, as_of_event_position, latest_event_type
            FROM compliance_audit_projection
            WHERE application_id = $1
            ORDER BY as_of_event_position
            """,
            app_id,
        )
        if not rows:
            print(f"No rows for application_id={app_id!r}")
            return
        for r in rows:
            print(json.dumps(dict(r), default=str, indent=2))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
