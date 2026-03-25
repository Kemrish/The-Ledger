"""
Phase 2: Command handlers and aggregates — submit application, start session, credit analysis, decision flow.
"""
import os

import pytest
import asyncpg

from src.event_store import EventStore
from src.commands.handlers import (
    handle_submit_application,
    handle_start_agent_session,
    handle_credit_analysis_completed,
    handle_fraud_screening_completed,
    handle_generate_decision,
    handle_human_review_completed,
)
from src.commands.models import (
    SubmitApplicationCommand,
    StartAgentSessionCommand,
    CreditAnalysisCompletedCommand,
    FraudScreeningCompletedCommand,
    GenerateDecisionCommand,
    HumanReviewCompletedCommand,
)
from src.aggregates.loan_application import LoanApplicationAggregate, ApplicationState


async def apply_schema(conn) -> None:
    schema_path = os.path.join(os.path.dirname(__file__), "..", "src", "schema.sql")
    with open(schema_path) as f:
        await conn.execute(f.read())


@pytest.fixture
async def store():
    dsn = os.environ.get(
        "LEDGER_TEST_DSN",
        "postgresql://postgres:postgres@localhost:5432/ledger_test",
    )
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")
    await apply_schema(conn)
    yield EventStore(conn)
    await conn.close()


def test_loan_aggregate_confidence_floor_coerces_refer():
    app = LoanApplicationAggregate(application_id="x")
    assert app.resolve_decision_recommendation("APPROVE", 0.59) == "REFER"
    assert app.resolve_decision_recommendation("approve", 0.6) == "APPROVE"
    assert app.resolve_decision_recommendation("DECLINE", 0.65) == "DECLINE"


@pytest.mark.asyncio
async def test_submit_and_load_aggregate(store):
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id="app-1",
            applicant_id="user-1",
            requested_amount_usd=50_000.0,
            loan_purpose="working_capital",
            submission_channel="api",
        ),
        store,
    )
    app = await LoanApplicationAggregate.load(store, "app-1")
    assert app.state == ApplicationState.SUBMITTED
    assert app.applicant_id == "user-1"
    assert app.requested_amount_usd == 50_000.0
    assert app.version == 1


@pytest.mark.asyncio
async def test_full_flow_credit_fraud_decision_review(store):
    import uuid
    app_id = f"app-flow-{uuid.uuid4().hex[:8]}"
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id,
            applicant_id="user-1",
            requested_amount_usd=100_000.0,
            loan_purpose="equipment",
        ),
        store,
    )

    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_id="credit",
            session_id="s1",
            context_source="replay",
            model_version="v2.0",
        ),
        store,
    )
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_id="fraud",
            session_id="s1",
            context_source="replay",
            model_version="v1.0",
        ),
        store,
    )

    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id,
            agent_id="credit",
            session_id="s1",
            model_version="v2.0",
            confidence_score=0.9,
            risk_tier="MEDIUM",
            recommended_limit_usd=90_000.0,
            duration_ms=100,
        ),
        store,
    )
    await handle_fraud_screening_completed(
        FraudScreeningCompletedCommand(
            application_id=app_id,
            agent_id="fraud",
            session_id="s1",
            fraud_score=0.1,
            screening_model_version="v1.0",
            input_data_hash="h1",
        ),
        store,
    )

    app = await LoanApplicationAggregate.load(store, app_id)
    assert app.state == ApplicationState.ANALYSIS_COMPLETE

    await handle_generate_decision(
        GenerateDecisionCommand(
            application_id=app_id,
            orchestrator_agent_id="orchestrator",
            session_id="s1",
            recommendation="APPROVE",
            confidence_score=0.85,
            contributing_agent_sessions=["agent-credit-s1", "agent-fraud-s1"],
            decision_basis_summary="Credit MEDIUM, fraud low.",
            model_versions={"credit": "v2.0", "fraud": "v1.0"},
        ),
        store,
    )

    app = await LoanApplicationAggregate.load(store, app_id)
    assert app.state == ApplicationState.PENDING_DECISION

    await handle_human_review_completed(
        HumanReviewCompletedCommand(
            application_id=app_id,
            reviewer_id="human-1",
            override=False,
            final_decision="APPROVE",
        ),
        store,
    )

    app = await LoanApplicationAggregate.load(store, app_id)
    assert app.state == ApplicationState.APPROVED_PENDING_HUMAN
