import os

import asyncpg
import pytest

from src.event_store import EventStore
from src.models.events import ApplicationSubmitted
from src.projections.daemon import ProjectionDaemon
from src.projections.application_summary import ApplicationSummaryProjection


async def apply_schema(conn):
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "src", "schema.sql")) as f:
        await conn.execute(f.read())


@pytest.mark.asyncio
async def test_projection_daemon_processes_events():
    dsn = os.environ.get("LEDGER_TEST_DSN", "postgresql://postgres:postgres@localhost:5432/ledger_test")
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")

    await apply_schema(conn)
    store = EventStore(conn)

    await store.append(
        "loan-proj-1",
        [ApplicationSubmitted(
            application_id="proj-1",
            applicant_id="a1",
            requested_amount_usd=1000.0,
            loan_purpose="ops",
            submission_channel="api",
            submitted_at="2026-01-01T00:00:00Z",
        )],
        expected_version=-1,
    )

    daemon = ProjectionDaemon(store, [ApplicationSummaryProjection()])
    await daemon._process_batch()

    row = await conn.fetchrow("SELECT application_id, state FROM application_summary_projection WHERE application_id = 'proj-1'")
    assert row is not None
    assert row["state"] == "Submitted"

    await conn.close()
