import os

import asyncpg
import pytest

from src.event_store import EventStore
from src.mcp import tools
from src.mcp import resources


async def apply_schema(conn):
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "src", "schema.sql")) as f:
        await conn.execute(f.read())


@pytest.mark.asyncio
async def test_mcp_tools_submit_and_start_session():
    dsn = os.environ.get("LEDGER_TEST_DSN", "postgresql://postgres:postgres@localhost:5432/ledger_test")
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")

    await apply_schema(conn)
    store = EventStore(conn)

    app_id = "mcp-1"
    res = await tools.submit_application(store, {
        "application_id": app_id,
        "applicant_id": "u1",
        "requested_amount_usd": 10000,
        "loan_purpose": "growth",
    })
    assert res["stream_id"] == f"loan-{app_id}"

    ses = await tools.start_agent_session(store, {
        "agent_id": "credit",
        "session_id": "s1",
        "context_source": "fresh",
        "model_version": "v1",
    })
    assert ses["session_id"] == "agent-credit-s1"

    ses2 = await tools.start_agent_session(store, {
        "agent_id": "fraud",
        "session_id": "s1",
        "context_source": "fresh",
        "model_version": "v1",
    })
    assert ses2["session_id"] == "agent-fraud-s1"

    credit = await tools.record_credit_analysis(store, {
        "application_id": app_id,
        "agent_id": "credit",
        "session_id": "s1",
        "model_version": "v1",
        "confidence_score": 0.85,
        "risk_tier": "LOW",
        "recommended_limit_usd": 8500,
        "duration_ms": 120,
        "input_data": {"x": 1},
    })
    assert credit.get("ok") is True

    fraud = await tools.record_fraud_screening(store, {
        "application_id": app_id,
        "agent_id": "fraud",
        "session_id": "s1",
        "fraud_score": 0.1,
        "anomaly_flags": [],
        "screening_model_version": "v1",
        "input_data_hash": "h",
    })
    assert fraud.get("ok") is True

    decision = await tools.generate_decision(store, {
        "application_id": app_id,
        "orchestrator_agent_id": "orch-1",
        "session_id": "s1",
        "recommendation": "APPROVE",
        "confidence_score": 0.92,
        "contributing_agent_sessions": ["agent-credit-s1", "agent-fraud-s1"],
        "decision_basis_summary": "Healthy metrics, low fraud risk.",
        "model_versions": {"credit": "v1", "fraud": "v1"},
    })
    assert decision.get("ok") is True

    review = await tools.record_human_review(store, {
        "application_id": app_id,
        "reviewer_id": "human-1",
        "override": False,
        "final_decision": "APPROVE",
    })
    assert review.get("ok") is True

    # This does not verify chain validity semantics beyond appendability, but ensures tool path works end-to-end.
    integrity = await tools.run_integrity_check_tool(store, {"entity_type": "loan", "entity_id": app_id})
    assert integrity.get("chain_valid") is True

    # Resources should return serializable dict/list values (projections).
    app_res = await resources.application_resource(store, app_id)
    assert app_res is None or isinstance(app_res, dict)

    comp_res = await resources.compliance_resource(store, app_id)
    assert comp_res is None or isinstance(comp_res, dict)

    await conn.close()
