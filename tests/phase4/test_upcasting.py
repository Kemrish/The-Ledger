import json
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
        model_version="v1",
        confidence_score=None,
        risk_tier="LOW",
        recommended_limit_usd=1000.0,
        analysis_duration_ms=10,
        input_data_hash="h",
    )
    await store.append(stream_id, [event], expected_version=-1)

    raw_before = await conn.fetchrow(
        "SELECT payload, event_version, event_id FROM events WHERE stream_id = $1 ORDER BY stream_position ASC LIMIT 1",
        stream_id,
    )
    assert raw_before["event_version"] == 1
    frozen = json.dumps(raw_before["payload"], sort_keys=True)

    loaded = await store.load_stream(stream_id)
    assert loaded[0].event_version >= 1

    raw_after = await conn.fetchrow(
        "SELECT payload, event_version, event_id FROM events WHERE stream_id = $1 ORDER BY stream_position ASC LIMIT 1",
        stream_id,
    )
    assert raw_after["event_version"] == 1
    assert raw_after["event_id"] == raw_before["event_id"]
    assert json.dumps(raw_after["payload"], sort_keys=True) == frozen

    await conn.close()


@pytest.mark.asyncio
async def test_raw_v1_row_unchanged_and_upcast_uses_null_for_unknown():
    """Prove DB row is untouched and in-memory upcast uses null (not fabricated strings)."""
    dsn = os.environ.get("LEDGER_TEST_DSN", "postgresql://postgres:postgres@localhost:5432/ledger_test")
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")

    await apply_schema(conn)
    store = create_store_with_upcasters(conn)

    stream_id = "loan-upcast-raw-v1"
    v1_minimal = {
        "application_id": "raw-1",
        "agent_id": "ag",
        "session_id": "sess",
        "risk_tier": "LOW",
        "recommended_limit_usd": 1000.0,
        "analysis_duration_ms": 10,
        "input_data_hash": "abc",
    }
    await conn.execute(
        """
        INSERT INTO event_streams (stream_id, aggregate_type, current_version)
        VALUES ($1, 'LoanApplication', 1)
        ON CONFLICT (stream_id) DO UPDATE SET current_version = 1
        """,
        stream_id,
    )
    await conn.execute(
        """
        INSERT INTO events (stream_id, stream_position, event_type, event_version, payload, metadata)
        VALUES ($1, 0, 'CreditAnalysisCompleted', 1, $2::jsonb, '{}'::jsonb)
        """,
        stream_id,
        json.dumps(v1_minimal),
    )

    raw_before = await conn.fetchrow(
        "SELECT payload::text AS ptext, event_version FROM events WHERE stream_id = $1",
        stream_id,
    )
    assert raw_before["event_version"] == 1

    loaded = await store.load_stream(stream_id)
    assert loaded[0].event_version == 2
    assert loaded[0].payload.get("model_version") is None
    assert loaded[0].payload.get("regulatory_basis") is None

    raw_after = await conn.fetchrow(
        "SELECT payload::text AS ptext, event_version FROM events WHERE stream_id = $1",
        stream_id,
    )
    assert raw_after["ptext"] == raw_before["ptext"]
    assert raw_after["event_version"] == 1

    await conn.close()
