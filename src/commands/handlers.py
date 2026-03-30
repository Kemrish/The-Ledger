"""
Command handlers: load → validate → determine events → append.

Each handler follows the same phases:
  1. Load — hydrate aggregates and/or stream version from the event store.
  2. Validate — invariants, state machine preconditions, and input guards (DomainError).
  3. Determine — build the list of domain events to persist (pure data).
  4. Append — optimistic concurrency write (and outbox in the same transaction per stream).

Business rules live on aggregates or in the validate step; append is never used to “fix” state.
"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from ..models.events import (
    ApplicationSubmitted,
    CreditAnalysisRequested,
    CreditAnalysisCompleted,
    FraudScreeningCompleted,
    PolicyEvaluationCompleted,
    DecisionGenerated,
    HumanReviewCompleted,
    ApplicationApproved,
    ApplicationDeclined,
    AgentContextLoaded,
    BaseEvent,
    DomainError,
)
from ..agents.policy_limits import evaluate_bank_policy
from ..aggregates.agent_session import AgentSessionAggregate
from ..aggregates.loan_application import LoanApplicationAggregate
from ..aggregates.compliance_record import ComplianceRecordAggregate
from .models import (
    SubmitApplicationCommand,
    CreditAnalysisCompletedCommand,
    FraudScreeningCompletedCommand,
    PolicyEvaluationCompletedCommand,
    ComplianceCheckCommand,
    GenerateDecisionCommand,
    HumanReviewCompletedCommand,
    StartAgentSessionCommand,
)

if TYPE_CHECKING:
    from ..event_store import EventStore


def hash_inputs(data: dict | None) -> str:
    if not data:
        return hashlib.sha256(b"").hexdigest()
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


async def _append_stream(
    store: "EventStore",
    stream_id: str,
    events: list[BaseEvent],
    expected_version: int,
    *,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> int:
    """Single append with optional correlation metadata (outbox + events metadata)."""
    return await store.append(
        stream_id,
        events,
        expected_version=expected_version,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )


async def handle_submit_application(cmd: SubmitApplicationCommand, store: EventStore) -> tuple[str, int]:
    """Create loan stream with ApplicationSubmitted; duplicate application_id is rejected."""
    stream_id = f"loan-{cmd.application_id}"
    # Load
    version = await store.stream_version(stream_id)
    # Validate (BR1)
    LoanApplicationAggregate.br1_enforce_unique_application(version, cmd.application_id)
    # Determine
    events = [
        ApplicationSubmitted(
            application_id=cmd.application_id,
            applicant_id=cmd.applicant_id,
            requested_amount_usd=cmd.requested_amount_usd,
            loan_purpose=cmd.loan_purpose,
            submission_channel=cmd.submission_channel,
            submitted_at=cmd.submitted_at_value(),
        )
    ]
    # Append
    new_version = await _append_stream(store, stream_id, events, expected_version=-1)
    return stream_id, new_version


async def handle_credit_analysis_completed(cmd: CreditAnalysisCompletedCommand, store: EventStore) -> None:
    """Persist credit analysis on agent stream then loan stream (same event body on both)."""
    # Load
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    agent = await AgentSessionAggregate.load(store, cmd.agent_id, cmd.session_id)
    # Validate — BR2 + AgentSession
    app.br2_enforce_credit_analysis_eligibility()
    agent.assert_context_loaded()
    agent.assert_model_version_current(cmd.model_version)
    # Determine
    input_hash = hash_inputs(cmd.input_data)
    event = CreditAnalysisCompleted(
        application_id=cmd.application_id,
        agent_id=cmd.agent_id,
        session_id=cmd.session_id,
        model_version=cmd.model_version,
        confidence_score=cmd.confidence_score,
        risk_tier=cmd.risk_tier,
        recommended_limit_usd=cmd.recommended_limit_usd,
        analysis_duration_ms=cmd.duration_ms,
        input_data_hash=input_hash,
    )
    agent_stream_id = f"agent-{cmd.agent_id}-{cmd.session_id}"
    loan_stream_id = f"loan-{cmd.application_id}"
    # Append (agent stream first, then loan — separate transactions; callers may retry loan on OCE)
    await _append_stream(
        store,
        agent_stream_id,
        [event],
        expected_version=agent.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )
    await _append_stream(
        store,
        loan_stream_id,
        [event],
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_fraud_screening_completed(cmd: FraudScreeningCompletedCommand, store: EventStore) -> None:
    # Load
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    agent = await AgentSessionAggregate.load(store, cmd.agent_id, cmd.session_id)
    # Validate
    agent.assert_context_loaded()
    LoanApplicationAggregate.enforce_fraud_score_range(cmd.fraud_score)
    # Determine
    event = FraudScreeningCompleted(
        application_id=cmd.application_id,
        agent_id=cmd.agent_id,
        fraud_score=cmd.fraud_score,
        anomaly_flags=cmd.anomaly_flags,
        screening_model_version=cmd.screening_model_version,
        input_data_hash=cmd.input_data_hash or hash_inputs(None),
    )
    agent_stream_id = f"agent-{cmd.agent_id}-{cmd.session_id}"
    loan_stream_id = f"loan-{cmd.application_id}"
    # Append
    await _append_stream(
        store,
        agent_stream_id,
        [event],
        expected_version=agent.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )
    await _append_stream(
        store,
        loan_stream_id,
        [event],
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_policy_evaluation_completed(cmd: PolicyEvaluationCompletedCommand, store: EventStore) -> None:
    """Internal bank policy evaluation on agent stream then loan stream."""
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    agent = await AgentSessionAggregate.load(store, cmd.agent_id, cmd.session_id)
    app.br_policy_enforce_evaluation_eligibility()
    agent.assert_context_loaded()
    agent.assert_model_version_current(cmd.model_version)

    outcome = evaluate_bank_policy(
        loan_purpose=cmd.loan_purpose,
        requested_amount_usd=cmd.requested_amount_usd,
        risk_tier=cmd.risk_tier,
        fraud_score=cmd.fraud_score,
    )
    input_hash = hash_inputs(cmd.input_data)
    event = PolicyEvaluationCompleted(
        application_id=cmd.application_id,
        agent_id=cmd.agent_id,
        session_id=cmd.session_id,
        model_version=cmd.model_version,
        policy_set_version=outcome["policy_set_version"],
        policy_passed=bool(outcome["policy_passed"]),
        recommended_action=str(outcome["recommended_action"]),
        policy_violations=list(outcome.get("policy_violations") or []),
        evaluation_duration_ms=cmd.duration_ms,
        input_data_hash=input_hash,
    )
    agent_stream_id = f"agent-{cmd.agent_id}-{cmd.session_id}"
    loan_stream_id = f"loan-{cmd.application_id}"
    await _append_stream(
        store,
        agent_stream_id,
        [event],
        expected_version=agent.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )
    await _append_stream(
        store,
        loan_stream_id,
        [event],
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_compliance_check(cmd: ComplianceCheckCommand, store: EventStore) -> None:
    # Load
    comp = await ComplianceRecordAggregate.load(store, cmd.application_id)
    expected = comp.version
    # Validate / determine
    events = comp.build_events_for_command(cmd)
    if not events:
        return
    stream_id = f"compliance-{cmd.application_id}"
    ev_version = -1 if expected == 0 else expected
    # Append
    await _append_stream(
        store,
        stream_id,
        events,
        expected_version=ev_version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_generate_decision(cmd: GenerateDecisionCommand, store: EventStore) -> None:
    # Load
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    agents = await LoanApplicationAggregate.load_contributing_agent_sessions(
        store, cmd.contributing_agent_sessions
    )
    # Validate — BR3, BR6
    app.br3_enforce_analyses_complete_for_decision()
    app.br6_enforce_contributing_sessions_have_analysis(agents)
    recommendation = app.br6_resolve_recommendation_with_confidence_floor(
        cmd.recommendation, cmd.confidence_score
    )
    # Determine
    event = DecisionGenerated(
        application_id=cmd.application_id,
        orchestrator_agent_id=cmd.orchestrator_agent_id,
        recommendation=recommendation,
        confidence_score=cmd.confidence_score,
        contributing_agent_sessions=cmd.contributing_agent_sessions,
        decision_basis_summary=cmd.decision_basis_summary,
        model_versions=cmd.model_versions,
    )
    # Append
    await _append_stream(
        store,
        f"loan-{cmd.application_id}",
        [event],
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_human_review_completed(cmd: HumanReviewCompletedCommand, store: EventStore) -> None:
    # Load
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    # Validate
    app.enforce_human_review_command(cmd.override, cmd.override_reason)
    # Determine
    event = HumanReviewCompleted(
        application_id=cmd.application_id,
        reviewer_id=cmd.reviewer_id,
        override=cmd.override,
        final_decision=cmd.final_decision.upper(),
        override_reason=cmd.override_reason,
    )
    # Append
    await _append_stream(store, f"loan-{cmd.application_id}", [event], expected_version=app.version)


async def handle_application_approved(
    application_id: str,
    approved_amount_usd: float,
    interest_rate: float,
    conditions: list[str],
    approved_by: str,
    effective_date: str,
    store: EventStore,
) -> None:
    # Load
    app = await LoanApplicationAggregate.load(store, application_id)
    # Validate — BR4, BR5
    app.enforce_funding_approval_command(approved_amount_usd)
    # Determine
    event = ApplicationApproved(
        application_id=application_id,
        approved_amount_usd=approved_amount_usd,
        interest_rate=interest_rate,
        conditions=conditions,
        approved_by=approved_by,
        effective_date=effective_date,
    )
    # Append
    await _append_stream(store, f"loan-{application_id}", [event], expected_version=app.version)


async def handle_application_declined(
    application_id: str,
    decline_reasons: list[str],
    declined_by: str,
    adverse_action_notice_required: bool,
    store: EventStore,
) -> None:
    # Load
    app = await LoanApplicationAggregate.load(store, application_id)
    # Validate
    app.enforce_final_decline_command()
    # Determine
    event = ApplicationDeclined(
        application_id=application_id,
        decline_reasons=decline_reasons,
        declined_by=declined_by,
        adverse_action_notice_required=adverse_action_notice_required,
    )
    # Append
    await _append_stream(store, f"loan-{application_id}", [event], expected_version=app.version)


async def handle_start_agent_session(cmd: StartAgentSessionCommand, store: EventStore) -> tuple[str, int]:
    """Gas Town: AgentContextLoaded must be the first event on the agent session stream."""
    stream_id = f"agent-{cmd.agent_id}-{cmd.session_id}"
    # Load
    version = await store.stream_version(stream_id)
    # Validate
    AgentSessionAggregate.enforce_new_session_stream(version, stream_id)
    # Determine
    event = AgentContextLoaded(
        agent_id=cmd.agent_id,
        session_id=cmd.session_id,
        context_source=cmd.context_source,
        event_replay_from_position=cmd.event_replay_from_position,
        context_token_count=cmd.context_token_count,
        model_version=cmd.model_version,
    )
    # Append
    new_version = await _append_stream(store, stream_id, [event], expected_version=-1)
    return stream_id, new_version
