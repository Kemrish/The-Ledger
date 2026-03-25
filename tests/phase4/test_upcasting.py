import os

import asyncpg
import pytest

from src.models.events import CreditAnalysisCompleted
from src.upcasting.upcasters import create_store_with_upcasters


async def apply_schema(conn) -> None:
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "src", "schema.sql")) as f:
        await conn.execute(f.read())


@pytest.mark.asyncio
async def test_upcasting_does_not_mutate_stored_payload():
    dsn = os.environ.get("LEDGER_TEST_DSN", "postgresql://postgres:postgres@localhost:5432/ledger_test")
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")

    await apply_schema(conn)
    store = create_store_with_upcasters(conn)

    stream_id = "loan-upcast-1"
    event = CreditAnalysisCompleted(
        application_id="upcast-1",
        agent_id="agent-x",
        session_id="session-x",
        model_version="legacy-pre-2026",
        confidence_score=None,
        risk_tier="LOW",
        recommended_limit_usd=1000.0,
        analysis_duration_ms=10,
        input_data_hash="h",
    )
    await store.append(stream_id, [event], expected_version=-1)

    raw_before = await conn.fetchrow(
        "SELECT payload, event_version FROM events WHERE stream_id = $1 ORDER BY stream_position ASC LIMIT 1",
        stream_id,
    )
    assert raw_before["event_version"] == 1

    loaded = await store.load_stream(stream_id)
    assert loaded[0].event_version >= 1

    raw_after = await conn.fetchrow(
        "SELECT payload, event_version FROM events WHERE stream_id = $1 ORDER BY stream_position ASC LIMIT 1",
        stream_id,
    )
    assert raw_after["event_version"] == 1
    assert raw_after["payload"] == raw_before["payload"]

    await conn.close()
