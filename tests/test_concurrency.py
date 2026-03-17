"""
Double-decision concurrency test (Phase 1).
Two AI agents simultaneously append to the same stream at expected_version=3.
Exactly one must succeed; the other must receive OptimisticConcurrencyError.
Total events in stream must be 4 (not 5).
"""
import asyncio
import os
import uuid

import pytest
import asyncpg

from src.event_store import EventStore
from src.models.events import CreditAnalysisCompleted, OptimisticConcurrencyError


async def apply_schema(conn) -> None:
    schema_path = os.path.join(os.path.dirname(__file__), "..", "src", "schema.sql")
    with open(schema_path) as f:
        await conn.execute(f.read())


@pytest.fixture
async def dsn_and_schema():
    dsn = os.environ.get(
        "LEDGER_TEST_DSN",
        "postgresql://postgres:postgres@localhost:5432/ledger_test",
    )
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")
    await apply_schema(conn)
    await conn.close()
    yield dsn


@pytest.mark.asyncio
async def test_double_decision_concurrency(dsn_and_schema):
    """Two agents both append at expected_version=3; one wins, one gets OptimisticConcurrencyError; stream has 4 events."""
    dsn = dsn_and_schema

    app_id = f"test-app-{uuid.uuid4().hex[:12]}"
    stream_id = f"loan-{app_id}"
    # Seed stream with 3 events so current_version = 3 (single connection)
    conn_seed = await asyncpg.connect(dsn)
    store_seed = EventStore(conn_seed)
    # Seed stream with 3 events so current_version = 3
    for i in range(3):
        ev = CreditAnalysisCompleted(
            application_id=app_id,
            agent_id=f"agent-{i}",
            session_id=f"session-{i}",
            model_version="v1",
            confidence_score=0.9,
            risk_tier="LOW",
            recommended_limit_usd=100_000.0,
            analysis_duration_ms=50,
            input_data_hash="abc",
        )
        await store_seed.append(
            stream_id,
            [ev],
            expected_version=-1 if i == 0 else i,
        )

    version_after_seed = await store_seed.stream_version(stream_id)
    assert version_after_seed == 3, "Stream must have 3 events before concurrent append"
    await conn_seed.close()

    winner_result: list[int] = []
    loser_error: list[Exception] = []

    async def agent_a():
        conn_a = await asyncpg.connect(dsn)
        store_a = EventStore(conn_a)
        try:
            v = await store_a.append(
                stream_id,
                [
                    CreditAnalysisCompleted(
                        application_id=app_id,
                        agent_id="agent-a",
                        session_id="session-a",
                        model_version="v1",
                        confidence_score=0.85,
                        risk_tier="MEDIUM",
                        recommended_limit_usd=80_000.0,
                        analysis_duration_ms=60,
                        input_data_hash="a",
                    )
                ],
                expected_version=3,
            )
            winner_result.append(v)
        except OptimisticConcurrencyError as e:
            loser_error.append(e)
        finally:
            await conn_a.close()

    async def agent_b():
        conn_b = await asyncpg.connect(dsn)
        store_b = EventStore(conn_b)
        try:
            v = await store_b.append(
                stream_id,
                [
                    CreditAnalysisCompleted(
                        application_id=app_id,
                        agent_id="agent-b",
                        session_id="session-b",
                        model_version="v1",
                        confidence_score=0.88,
                        risk_tier="MEDIUM",
                        recommended_limit_usd=75_000.0,
                        analysis_duration_ms=55,
                        input_data_hash="b",
                    )
                ],
                expected_version=3,
            )
            winner_result.append(v)
        except OptimisticConcurrencyError as e:
            loser_error.append(e)
        finally:
            await conn_b.close()

    await asyncio.gather(agent_a(), agent_b())

    # (a) Total events in stream = 4 (not 5)
    conn_check = await asyncpg.connect(dsn)
    store_check = EventStore(conn_check)
    final_version = await store_check.stream_version(stream_id)
    await conn_check.close()
    assert final_version == 4, f"Expected 4 events in stream, got {final_version}"

    # (b) Exactly one task succeeded and got new version 4
    assert len(winner_result) == 1 and winner_result[0] == 4

    # (c) The other received OptimisticConcurrencyError (not silently swallowed)
    assert len(loser_error) == 1
    assert loser_error[0].expected_version == 3
    assert loser_error[0].actual_version == 4
    assert loser_error[0].suggested_action == "reload_stream_and_retry"
