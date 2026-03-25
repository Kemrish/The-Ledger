import os

import asyncpg
import pytest

from src.event_store import EventStore
from src.models.events import AgentContextLoaded, CreditAnalysisCompleted
from src.integrity.gas_town import reconstruct_agent_context


async def apply_schema(conn):
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "src", "schema.sql")) as f:
        await conn.execute(f.read())


@pytest.mark.asyncio
async def test_reconstruct_agent_context_after_crash():
    dsn = os.environ.get("LEDGER_TEST_DSN", "postgresql://postgres:postgres@localhost:5432/ledger_test")
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")

    await apply_schema(conn)
    store = EventStore(conn)
    stream = "agent-credit-s-crash"

    await store.append(
        stream,
        [AgentContextLoaded(
            agent_id="credit",
            session_id="s-crash",
            context_source="replay",
            event_replay_from_position=0,
            context_token_count=200,
            model_version="v2"
        )],
        expected_version=-1,
    )
    for i in range(4):
        await store.append(
            stream,
            [CreditAnalysisCompleted(
                application_id=f"app-{i}",
                agent_id="credit",
                session_id="s-crash",
                model_version="v2",
                confidence_score=0.8,
                risk_tier="LOW",
                recommended_limit_usd=5000.0,
                analysis_duration_ms=20,
                input_data_hash="h",
            )],
            expected_version=i + 1,
        )

    ctx = await reconstruct_agent_context(store, "credit", "s-crash")
    assert "Session agent-credit-s-crash replayed: 5 events." in ctx.context_text
    assert "Last 3 events (verbatim payloads):" in ctx.context_text
    assert ctx.last_event_position == 4
    assert ctx.session_health_status in {"HEALTHY", "NEEDS_RECONCILIATION"}

    await conn.close()
