"""
LoanApplication aggregate: full lifecycle from submission to final decision.
State machine and business rules enforced here; event replay via load() and _apply().
"""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from ..models.events import StoredEvent, DomainError
from .agent_session import AgentSessionAggregate

if TYPE_CHECKING:
    from ..event_store import EventStore


class ApplicationState(str, Enum):
    SUBMITTED = "Submitted"
    AWAITING_ANALYSIS = "AwaitingAnalysis"
    ANALYSIS_COMPLETE = "AnalysisComplete"
    COMPLIANCE_REVIEW = "ComplianceReview"
    PENDING_DECISION = "PendingDecision"
    APPROVED_PENDING_HUMAN = "ApprovedPendingHuman"
    DECLINED_PENDING_HUMAN = "DeclinedPendingHuman"
    FINAL_APPROVED = "FinalApproved"
    FINAL_DECLINED = "FinalDeclined"


# Valid transitions (from -> allowed to)
VALID_TRANSITIONS: dict[ApplicationState, set[ApplicationState]] = {
    ApplicationState.SUBMITTED: {ApplicationState.AWAITING_ANALYSIS},
    ApplicationState.AWAITING_ANALYSIS: {ApplicationState.ANALYSIS_COMPLETE},
    ApplicationState.ANALYSIS_COMPLETE: {ApplicationState.COMPLIANCE_REVIEW, ApplicationState.PENDING_DECISION},
    ApplicationState.COMPLIANCE_REVIEW: {ApplicationState.PENDING_DECISION},
    ApplicationState.PENDING_DECISION: {
        ApplicationState.APPROVED_PENDING_HUMAN,
        ApplicationState.DECLINED_PENDING_HUMAN,
    },
    ApplicationState.APPROVED_PENDING_HUMAN: {ApplicationState.FINAL_APPROVED},
    ApplicationState.DECLINED_PENDING_HUMAN: {ApplicationState.FINAL_DECLINED},
    ApplicationState.FINAL_APPROVED: set(),
    ApplicationState.FINAL_DECLINED: set(),
}


class LoanApplicationAggregate:
    """Reconstructed by replaying loan-{application_id} stream."""

    # Orchestrator outputs below this confidence must be treated as REFER (policy / risk).
    DECISION_CONFIDENCE_FLOOR: float = 0.6

    def __init__(self, application_id: str) -> None:
        self.application_id = application_id
        self.version: int = 0
        self.state = ApplicationState.SUBMITTED
        self.applicant_id: str = ""
        self.requested_amount_usd: float = 0.0
        self.approved_amount_usd: float | None = None
        self.risk_tier: str | None = None
        self.recommended_limit_usd: float | None = None  # agent-assessed max; approved cannot exceed
        self.credit_analysis_done = False
        self.fraud_screening_done = False
        self.compliance_cleared = False  # Set when we have enough info (e.g. from compliance stream)
        self.decision_recommendation: str | None = None
        self.human_reviewer_id: str | None = None

    @classmethod
    async def load(cls, store: EventStore, application_id: str) -> LoanApplicationAggregate:
        events = await store.load_stream(f"loan-{application_id}")
        agg = cls(application_id=application_id)
        for event in events:
            agg._apply(event)
        # Compliance dependency: separate aggregate stream; merge for approval invariants.
        comp_events = await store.load_stream(f"compliance-{application_id}")
        if comp_events:
            required: set[str] = set()
            passed: set[str] = set()
            for e in comp_events:
                if e.event_type == "ComplianceCheckRequested":
                    required = set(e.payload.get("checks_required", []))
                elif e.event_type == "ComplianceRulePassed":
                    passed.add(e.payload.get("rule_id", ""))
            agg.compliance_cleared = bool(required and required.issubset(passed))
        return agg

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event)
        self.version = event.stream_position + 1  # version = number of events

    def _transition(self, new_state: ApplicationState) -> None:
        allowed = VALID_TRANSITIONS.get(self.state, set())
        if new_state not in allowed and new_state != self.state:
            raise DomainError(
                f"Invalid transition from {self.state} to {new_state}",
                code="INVALID_STATE_TRANSITION",
            )
        self.state = new_state

    def _on_ApplicationSubmitted(self, event: StoredEvent) -> None:
        p = event.payload
        self._transition(ApplicationState.SUBMITTED)
        self.applicant_id = p["applicant_id"]
        self.requested_amount_usd = float(p["requested_amount_usd"])

    def _on_CreditAnalysisRequested(self, event: StoredEvent) -> None:
        self._transition(ApplicationState.AWAITING_ANALYSIS)

    def _on_CreditAnalysisCompleted(self, event: StoredEvent) -> None:
        p = event.payload
        if self.state == ApplicationState.SUBMITTED:
            self._transition(ApplicationState.AWAITING_ANALYSIS)
        self.credit_analysis_done = True
        self.risk_tier = p.get("risk_tier")
        self.recommended_limit_usd = float(p.get("recommended_limit_usd", 0))
        if self.fraud_screening_done:
            self._transition(ApplicationState.ANALYSIS_COMPLETE)
        # else stay AwaitingAnalysis until fraud done

    def _on_FraudScreeningCompleted(self, event: StoredEvent) -> None:
        if self.state == ApplicationState.SUBMITTED:
            self._transition(ApplicationState.AWAITING_ANALYSIS)
        self.fraud_screening_done = True
        if self.credit_analysis_done:
            self._transition(ApplicationState.ANALYSIS_COMPLETE)

    def _on_DecisionGenerated(self, event: StoredEvent) -> None:
        p = event.payload
        self.decision_recommendation = p.get("recommendation")
        self._transition(ApplicationState.PENDING_DECISION)

    def _on_HumanReviewCompleted(self, event: StoredEvent) -> None:
        p = event.payload
        self.human_reviewer_id = p.get("reviewer_id")
        fd = (p.get("final_decision") or "").upper()
        if "APPROVE" in fd or fd == "APPROVED":
            self._transition(ApplicationState.APPROVED_PENDING_HUMAN)
        else:
            self._transition(ApplicationState.DECLINED_PENDING_HUMAN)

    def _on_ApplicationApproved(self, event: StoredEvent) -> None:
        p = event.payload
        self.approved_amount_usd = float(p["approved_amount_usd"])
        self._transition(ApplicationState.FINAL_APPROVED)

    def _on_ApplicationDeclined(self, event: StoredEvent) -> None:
        self._transition(ApplicationState.FINAL_DECLINED)

    # --- Business rule assertions (used by command handlers) ---

    def assert_awaiting_credit_analysis(self) -> None:
        if self.credit_analysis_done:
            raise DomainError(
                "Credit analysis already completed for this application",
                code="DUPLICATE_CREDIT_ANALYSIS",
            )
        if self.state not in (ApplicationState.SUBMITTED, ApplicationState.AWAITING_ANALYSIS):
            raise DomainError(
                f"Application not awaiting credit analysis; state={self.state}",
                code="INVALID_STATE",
            )

    def assert_analysis_complete(self) -> None:
        if not self.credit_analysis_done or not self.fraud_screening_done:
            raise DomainError(
                "Credit and fraud analysis must be complete before decision",
                code="ANALYSIS_INCOMPLETE",
            )

    def assert_compliance_cleared(self) -> None:
        if not self.compliance_cleared:
            raise DomainError(
                "Compliance checks must be cleared before approval",
                code="COMPLIANCE_PENDING",
            )

    def assert_can_approve(self, approved_amount_usd: float) -> None:
        if self.recommended_limit_usd is not None and approved_amount_usd > self.recommended_limit_usd:
            raise DomainError(
                f"Approved amount {approved_amount_usd} cannot exceed agent-assessed limit {self.recommended_limit_usd}",
                code="CREDIT_LIMIT_EXCEEDED",
            )
        self.assert_compliance_cleared()

    def assert_pending_decision(self) -> None:
        if self.state != ApplicationState.PENDING_DECISION:
            raise DomainError(
                f"Application not in PendingDecision; state={self.state}",
                code="INVALID_STATE",
            )

    def assert_approved_pending_human(self) -> None:
        if self.state != ApplicationState.APPROVED_PENDING_HUMAN:
            raise DomainError(
                f"Application not in ApprovedPendingHuman; state={self.state}",
                code="INVALID_STATE",
            )

    def assert_declined_pending_human(self) -> None:
        if self.state != ApplicationState.DECLINED_PENDING_HUMAN:
            raise DomainError(
                f"Application not in DeclinedPendingHuman; state={self.state}",
                code="INVALID_STATE",
            )

    def resolve_decision_recommendation(self, recommendation: str, confidence_score: float) -> str:
        """Apply confidence floor: low-confidence decisions are coerced to REFER."""
        r = (recommendation or "").strip().upper()
        if confidence_score < self.DECISION_CONFIDENCE_FLOOR:
            return "REFER"
        return r

    def assert_contributing_agent_sessions(self, sessions: list[AgentSessionAggregate]) -> None:
        """Each session must have produced analysis for this application (Gas Town / causal chain)."""
        for s in sessions:
            if not s.has_decision_for_application(self.application_id):
                raise DomainError(
                    f"Session agent-{s.agent_id}-{s.session_id} has no decision event for application {self.application_id}",
                    code="CAUSAL_CHAIN_VIOLATION",
                )
