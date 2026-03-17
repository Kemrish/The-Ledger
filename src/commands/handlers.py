"""
Command handlers: load → validate → determine events → append.
All business rules enforced in aggregates or here before any write.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..models.events import (
    ApplicationSubmitted,
    CreditAnalysisRequested,
    CreditAnalysisCompleted,
    FraudScreeningCompleted,
    ComplianceCheckRequested,
    ComplianceRulePassed,
    ComplianceRuleFailed,
    DecisionGenerated,
    HumanReviewCompleted,
    ApplicationApproved,
    ApplicationDeclined,
    AgentContextLoaded,
    DomainError,
)
from ..aggregates.loan_application import LoanApplicationAggregate
from ..aggregates.agent_session import AgentSessionAggregate
from ..aggregates.compliance_record import ComplianceRecordAggregate
from .models import (
    SubmitApplicationCommand,
    CreditAnalysisCompletedCommand,
    FraudScreeningCompletedCommand,
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


async def handle_submit_application(cmd: SubmitApplicationCommand, store: EventStore) -> tuple[str, int]:
    """Append ApplicationSubmitted. Fails if stream already exists (duplicate application_id)."""
    stream_id = f"loan-{cmd.application_id}"
    version = await store.stream_version(stream_id)
    if version != 0:
        raise DomainError(f"Application {cmd.application_id} already exists", code="DUPLICATE_APPLICATION")
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
    new_version = await store.append(stream_id, events, expected_version=-1)
    return stream_id, new_version


async def handle_credit_analysis_completed(cmd: CreditAnalysisCompletedCommand, store: EventStore) -> None:
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    agent = await AgentSessionAggregate.load(store, cmd.agent_id, cmd.session_id)

    app.assert_awaiting_credit_analysis()
    agent.assert_context_loaded()
    agent.assert_model_version_current(cmd.model_version)

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
    await store.append(
        agent_stream_id,
        [event],
        expected_version=agent.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )
    loan_stream_id = f"loan-{cmd.application_id}"
    await store.append(
        loan_stream_id,
        [event],
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_fraud_screening_completed(cmd: FraudScreeningCompletedCommand, store: EventStore) -> None:
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    agent = await AgentSessionAggregate.load(store, cmd.agent_id, cmd.session_id)

    agent.assert_context_loaded()
    if not 0.0 <= cmd.fraud_score <= 1.0:
        raise DomainError("fraud_score must be between 0.0 and 1.0", code="INVALID_FRAUD_SCORE")

    event = FraudScreeningCompleted(
        application_id=cmd.application_id,
        agent_id=cmd.agent_id,
        fraud_score=cmd.fraud_score,
        anomaly_flags=cmd.anomaly_flags,
        screening_model_version=cmd.screening_model_version,
        input_data_hash=cmd.input_data_hash or hash_inputs(None),
    )

    agent_stream_id = f"agent-{cmd.agent_id}-{cmd.session_id}"
    await store.append(
        agent_stream_id,
        [event],
        expected_version=agent.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )
    loan_stream_id = f"loan-{cmd.application_id}"
    await store.append(
        loan_stream_id,
        [event],
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_compliance_check(cmd: ComplianceCheckCommand, store: EventStore) -> None:
    comp = await ComplianceRecordAggregate.load(store, cmd.application_id)
    events: list = []
    expected = comp.version

    if not comp.checks_required and cmd.checks_required:
        events.append(
            ComplianceCheckRequested(
                application_id=cmd.application_id,
                regulation_set_version=cmd.regulation_set_version,
                checks_required=cmd.checks_required,
            )
        )
    if cmd.passed_rule_id:
        events.append(
            ComplianceRulePassed(
                application_id=cmd.application_id,
                rule_id=cmd.passed_rule_id,
                rule_version=cmd.passed_rule_version or "",
                evaluation_timestamp=datetime.now(timezone.utc).isoformat(),
                evidence_hash=cmd.passed_evidence_hash or "",
            )
        )
    if cmd.failed_rule_id:
        events.append(
            ComplianceRuleFailed(
                application_id=cmd.application_id,
                rule_id=cmd.failed_rule_id,
                rule_version=cmd.failed_rule_version or "",
                failure_reason=cmd.failure_reason or "",
                remediation_required=cmd.remediation_required,
            )
        )

    if not events:
        return
    stream_id = f"compliance-{cmd.application_id}"
    if expected == 0:
        expected = -1
    await store.append(
        stream_id,
        events,
        expected_version=expected,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_generate_decision(cmd: GenerateDecisionCommand, store: EventStore) -> None:
    app = await LoanApplicationAggregate.load(store, cmd.application_id)

    app.assert_analysis_complete()

    recommendation = cmd.recommendation.upper()
    if cmd.confidence_score < 0.6:
        recommendation = "REFER"

    for session_stream_id in cmd.contributing_agent_sessions:
        parts = session_stream_id.replace("agent-", "").split("-", 1)
        if len(parts) < 2:
            raise DomainError(
                f"Invalid contributing_agent_sessions entry: {session_stream_id}",
                code="INVALID_CAUSAL_CHAIN",
            )
        agent_id, session_id = parts[0], parts[1]
        agent = await AgentSessionAggregate.load(store, agent_id, session_id)
        if not agent.has_decision_for_application(cmd.application_id):
            raise DomainError(
                f"Session {session_stream_id} has no decision event for application {cmd.application_id}",
                code="CAUSAL_CHAIN_VIOLATION",
            )

    event = DecisionGenerated(
        application_id=cmd.application_id,
        orchestrator_agent_id=cmd.orchestrator_agent_id,
        recommendation=recommendation,
        confidence_score=cmd.confidence_score,
        contributing_agent_sessions=cmd.contributing_agent_sessions,
        decision_basis_summary=cmd.decision_basis_summary,
        model_versions=cmd.model_versions,
    )
    stream_id = f"loan-{cmd.application_id}"
    await store.append(
        stream_id,
        [event],
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_human_review_completed(cmd: HumanReviewCompletedCommand, store: EventStore) -> None:
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    app.assert_pending_decision()
    if cmd.override and not cmd.override_reason:
        raise DomainError("override_reason required when override=True", code="OVERRIDE_REASON_REQUIRED")

    event = HumanReviewCompleted(
        application_id=cmd.application_id,
        reviewer_id=cmd.reviewer_id,
        override=cmd.override,
        final_decision=cmd.final_decision.upper(),
        override_reason=cmd.override_reason,
    )
    stream_id = f"loan-{cmd.application_id}"
    await store.append(stream_id, [event], expected_version=app.version)


async def handle_application_approved(
    application_id: str,
    approved_amount_usd: float,
    interest_rate: float,
    conditions: list[str],
    approved_by: str,
    effective_date: str,
    store: EventStore,
) -> None:
    app = await LoanApplicationAggregate.load(store, application_id)
    app.assert_approved_pending_human()
    app.assert_can_approve(approved_amount_usd)

    event = ApplicationApproved(
        application_id=application_id,
        approved_amount_usd=approved_amount_usd,
        interest_rate=interest_rate,
        conditions=conditions,
        approved_by=approved_by,
        effective_date=effective_date,
    )
    await store.append(f"loan-{application_id}", [event], expected_version=app.version)


async def handle_application_declined(
    application_id: str,
    decline_reasons: list[str],
    declined_by: str,
    adverse_action_notice_required: bool,
    store: EventStore,
) -> None:
    app = await LoanApplicationAggregate.load(store, application_id)
    app.assert_declined_pending_human()

    event = ApplicationDeclined(
        application_id=application_id,
        decline_reasons=decline_reasons,
        declined_by=declined_by,
        adverse_action_notice_required=adverse_action_notice_required,
    )
    await store.append(f"loan-{application_id}", [event], expected_version=app.version)


async def handle_start_agent_session(cmd: StartAgentSessionCommand, store: EventStore) -> tuple[str, int]:
    """Gas Town: required before any agent decision tools."""
    stream_id = f"agent-{cmd.agent_id}-{cmd.session_id}"
    version = await store.stream_version(stream_id)
    if version != 0:
        raise DomainError(
            f"Agent session {stream_id} already exists",
            code="DUPLICATE_SESSION",
        )

    event = AgentContextLoaded(
        agent_id=cmd.agent_id,
        session_id=cmd.session_id,
        context_source=cmd.context_source,
        event_replay_from_position=cmd.event_replay_from_position,
        context_token_count=cmd.context_token_count,
        model_version=cmd.model_version,
    )
    new_version = await store.append(stream_id, [event], expected_version=-1)
    return stream_id, new_version
