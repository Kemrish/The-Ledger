"""
High-load SLO-style checks: concurrent command traffic, projection catch-up latency, rebuild-from-scratch.

Requires PostgreSQL (LEDGER_TEST_DSN).
"""
from __future__ import annotations

import asyncio
import os
import time

import asyncpg
import pytest

from src.commands.handlers import handle_submit_application
from src.commands.models import SubmitApplicationCommand
from src.event_store import EventStore
from src.projections.daemon import ProjectionDaemon
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.agent_performance import AgentPerformanceProjection
from src.projections.compliance_audit import ComplianceAuditProjection
from src.projections.rebuild import rebuild_projections_from_scratch


async def apply_schema(conn) -> None:
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "src", "schema.sql")) as f:
        await conn.execute(f.read())


@pytest.mark.asyncio
async def test_projection_catchup_and_rebuild_under_concurrent_submits():
    """
    Many concurrent appends (separate connections) → daemon processes batches → all lags zero.
    Rebuild truncates and replays; row counts match expected event volume.
    """
    dsn = os.environ.get("LEDGER_TEST_DSN", "postgresql://postgres:postgres@localhost:5432/ledger_test")
    try:
        pool = await asyncpg.create_pool(dsn, min_size=4, max_size=16)
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")

    async with pool.acquire() as setup:
        await apply_schema(setup)

    n = 36
    prefix = "slo-load"

    async def submit_one(i: int) -> None:
        async with pool.acquire() as conn:
            store = EventStore(conn)
            await handle_submit_application(
                SubmitApplicationCommand(
                    application_id=f"{prefix}-{i}",
                    applicant_id=f"u-{i}",
                    requested_amount_usd=10_000.0 + i,
                    loan_purpose="equipment",
                ),
                store,
            )

    t0 = time.perf_counter()
    await asyncio.gather(*[submit_one(i) for i in range(n)])
    ingest_s = time.perf_counter() - t0

    async with pool.acquire() as conn:
        store = EventStore(conn)
        projections = [
            ApplicationSummaryProjection(),
            AgentPerformanceProjection(),
            ComplianceAuditProjection(),
        ]
        daemon = ProjectionDaemon(store, projections)
        t1 = time.perf_counter()
        for _ in range(10_000):
            await daemon._process_batch()
            lags = await daemon.get_all_lags()
            if all(x.lag_events == 0 for x in lags):
                break
        else:
            pytest.fail("projections did not catch up within iteration budget")
        catchup_s = time.perf_counter() - t1

        # SLO: bounded catch-up after burst (tune if CI hardware is very slow)
        assert catchup_s < 60.0, f"catch-up too slow: {catchup_s}s (ingest parallel: {ingest_s:.2f}s)"
        assert ingest_s < 120.0

        row = await conn.fetchrow("SELECT COUNT(*) AS c FROM application_summary_projection WHERE application_id LIKE $1", f"{prefix}-%")
        assert row["c"] == n

        t2 = time.perf_counter()
        await rebuild_projections_from_scratch(store)
        rebuild_s = time.perf_counter() - t2
        assert rebuild_s < 120.0

        row2 = await conn.fetchrow("SELECT COUNT(*) AS c FROM application_summary_projection WHERE application_id LIKE $1", f"{prefix}-%")
        assert row2["c"] == n

    await pool.close()
