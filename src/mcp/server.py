from __future__ import annotations

import os

import asyncpg

from ..event_store import EventStore
from ..projections.daemon import ProjectionDaemon
from ..projections.application_summary import ApplicationSummaryProjection
from ..projections.agent_performance import AgentPerformanceProjection
from ..projections.compliance_audit import ComplianceAuditProjection


async def create_runtime():
    dsn = os.environ.get("LEDGER_TEST_DSN", "postgresql://postgres:postgres@localhost:5432/ledger_test")
    conn = await asyncpg.connect(dsn)
    store = EventStore(conn)
    daemon = ProjectionDaemon(
        store,
        [ApplicationSummaryProjection(), AgentPerformanceProjection(), ComplianceAuditProjection()],
    )
    return conn, store, daemon


if __name__ == "__main__":
    # Minimal executable entrypoint. Full FastMCP wiring is done in mcp/tools.py and mcp/resources.py.
    import asyncio

    async def _main():
        conn, _store, _daemon = await create_runtime()
        print("The Ledger MCP runtime initialized.")
        await conn.close()

    asyncio.run(_main())
