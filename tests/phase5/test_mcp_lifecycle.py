import os
import uuid

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

    app_id = f"mcp-{uuid.uuid4().hex[:12]}"
    sid = f"s-{uuid.uuid4().hex[:10]}"
    res = await tools.submit_application(store, {
        "application_id": app_id,
        "applicant_id": "u1",
        "requested_amount_usd": 10000,
        "loan_purpose": "growth",
    })
    assert res["stream_id"] == f"loan-{app_id}"

    ses = await tools.start_agent_session(store, {
        "agent_id": "credit",
        "session_id": sid,
        "context_source": "fresh",
        "model_version": "v1",
    })
    assert ses["session_id"] == f"agent-credit-{sid}"

    ses2 = await tools.start_agent_session(store, {
        "agent_id": "fraud",
        "session_id": sid,
        "context_source": "fresh",
        "model_version": "v1",
    })
    assert ses2["session_id"] == f"agent-fraud-{sid}"

    ses3 = await tools.start_agent_session(store, {
        "agent_id": "policy",
        "session_id": sid,
        "context_source": "fresh",
        "model_version": "v1",
    })
    assert ses3["session_id"] == f"agent-policy-{sid}"

    credit = await tools.record_credit_analysis(store, {
        "application_id": app_id,
        "agent_id": "credit",
        "session_id": sid,
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
        "session_id": sid,
        "fraud_score": 0.1,
        "anomaly_flags": [],
        "screening_model_version": "v1",
        "input_data_hash": "h",
    })
    assert fraud.get("ok") is True

    policy = await tools.record_policy_evaluation(store, {
        "application_id": app_id,
        "agent_id": "policy",
        "session_id": sid,
        "model_version": "v1",
        "loan_purpose": "growth",
        "requested_amount_usd": 10000,
        "risk_tier": "LOW",
        "fraud_score": 0.1,
        "duration_ms": 30,
        "input_data": {"agent": "policy_limits"},
    })
    assert policy.get("ok") is True

    decision = await tools.generate_decision(store, {
        "application_id": app_id,
        "orchestrator_agent_id": "orch-1",
        "session_id": sid,
        "recommendation": "APPROVE",
        "confidence_score": 0.92,
        "contributing_agent_sessions": [
            f"agent-credit-{sid}",
            f"agent-fraud-{sid}",
            f"agent-policy-{sid}",
        ],
        "decision_basis_summary": "Healthy metrics, low fraud risk.",
        "model_versions": {"credit": "v1", "fraud": "v1", "policy": "v1"},
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
