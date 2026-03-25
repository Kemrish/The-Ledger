"""
LoanApplication aggregate: full lifecycle from submission to final decision.

State machine and **six business rules** are enforced only through this aggregate (load → replay via
`load()` / `_apply()`; command validation via `br*` methods). Command handlers must not duplicate
these rules outside the aggregate.

**LoanApplication state machine** (ApplicationState)

Submitted → AwaitingAnalysis → AnalysisComplete → (optional ComplianceReview) → PendingDecision →
ApprovedPendingHuman | DeclinedPendingHuman → FinalApproved | FinalDeclined

**Six business rules (BR1–BR6)**

- **BR1 — Unique application:** A new loan stream may only be created when the stream does not exist.
- **BR2 — Credit analysis eligibility:** Credit analysis may be recorded at most once and only while
  the application is awaiting credit analysis (Submitted or AwaitingAnalysis).
- **BR3 — Analyses complete before decision:** Both credit and fraud analyses must be complete before
  an orchestrator decision is allowed.
- **BR4 — Compliance before funding:** Compliance must be cleared (from the ComplianceRecord stream)
  before any funding approval amount is accepted.
- **BR5 — Credit limit:** Approved amount cannot exceed the agent-assessed recommended limit.
- **BR6 — Orchestrator policy:** Contributing agent sessions must have produced analysis for this
  application (causal chain); recommendation must respect the confidence floor (coerce to REFER).
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
    """Reconstructed by replaying loan-{application_id} stream; compliance snapshot merged from compliance-{id}."""

    DECISION_CONFIDENCE_FLOOR: float = 0.6

    def __init__(self, application_id: str) -> None:
        self.application_id = application_id
        self.version: int = 0
        self.state = ApplicationState.SUBMITTED
        self.applicant_id: str = ""
        self.requested_amount_usd: float = 0.0
        self.approved_amount_usd: float | None = None
        self.risk_tier: str | None = None
        self.recommended_limit_usd: float | None = None
        self.credit_analysis_done = False
        self.fraud_screening_done = False
        self.compliance_cleared = False
        self.decision_recommendation: str | None = None
        self.human_reviewer_id: str | None = None

    @classmethod
    async def load_contributing_agent_sessions(
        cls, store: EventStore, contributing_stream_ids: list[str]
    ) -> list[AgentSessionAggregate]:
        """Parse `agent-{agentId}-{sessionId}` and load each AgentSession aggregate for BR6."""
        sessions: list[AgentSessionAggregate] = []
        for session_stream_id in contributing_stream_ids:
            parts = session_stream_id.replace("agent-", "").split("-", 1)
            if len(parts) < 2:
                raise DomainError(
                    f"Invalid contributing_agent_sessions entry: {session_stream_id}",
                    code="INVALID_CAUSAL_CHAIN",
                )
            agent_id, session_id = parts[0], parts[1]
            sessions.append(await AgentSessionAggregate.load(store, agent_id, session_id))
        return sessions

    @classmethod
    async def load(cls, store: EventStore, application_id: str) -> LoanApplicationAggregate:
        events = await store.load_stream(f"loan-{application_id}")
        agg = cls(application_id=application_id)
        for event in events:
            agg._apply(event)
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
        self.version = event.stream_position + 1

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

    # --- BR1: unique new application -------------------------------------------------

    @staticmethod
    def br1_enforce_unique_application(stream_version: int, application_id: str) -> None:
        """BR1: loan-{application_id} must not already exist."""
        if stream_version != 0:
            raise DomainError(
                f"Application {application_id} already exists",
                code="DUPLICATE_APPLICATION",
            )

    # --- BR2: credit analysis --------------------------------------------------------

    def br2_enforce_credit_analysis_eligibility(self) -> None:
        """BR2: at most one credit analysis; only while awaiting analysis."""
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

    # --- BR3: analyses before decision -----------------------------------------------

    def br3_enforce_analyses_complete_for_decision(self) -> None:
        """BR3: credit and fraud must both be complete."""
        if not self.credit_analysis_done or not self.fraud_screening_done:
            raise DomainError(
                "Credit and fraud analysis must be complete before decision",
                code="ANALYSIS_INCOMPLETE",
            )

    # --- BR4: compliance before funding ----------------------------------------------

    def br4_enforce_compliance_cleared_for_funding(self) -> None:
        """BR4: compliance cleared before disbursement approval."""
        if not self.compliance_cleared:
            raise DomainError(
                "Compliance checks must be cleared before approval",
                code="COMPLIANCE_PENDING",
            )

    # --- BR5: credit limit -----------------------------------------------------------

    def br5_enforce_approved_amount_within_agent_limit(self, approved_amount_usd: float) -> None:
        """BR5: approved amount cannot exceed agent recommended limit."""
        if self.recommended_limit_usd is not None and approved_amount_usd > self.recommended_limit_usd:
            raise DomainError(
                f"Approved amount {approved_amount_usd} cannot exceed agent-assessed limit {self.recommended_limit_usd}",
                code="CREDIT_LIMIT_EXCEEDED",
            )

    # --- BR6: orchestrator causal chain + confidence floor ---------------------------

    def br6_enforce_contributing_sessions_have_analysis(self, sessions: list[AgentSessionAggregate]) -> None:
        """BR6a: each contributing session must have analysis for this application."""
        for s in sessions:
            if not s.has_decision_for_application(self.application_id):
                raise DomainError(
                    f"Session agent-{s.agent_id}-{s.session_id} has no analysis for application {self.application_id}",
                    code="CAUSAL_CHAIN_VIOLATION",
                )

    def br6_resolve_recommendation_with_confidence_floor(
        self, recommendation: str, confidence_score: float
    ) -> str:
        """BR6b: low confidence forces REFER."""
        r = (recommendation or "").strip().upper()
        if confidence_score < self.DECISION_CONFIDENCE_FLOOR:
            return "REFER"
        return r

    # --- Human review invariants (state machine gate) -------------------------------

    def enforce_human_review_command(self, override: bool, override_reason: str | None) -> None:
        """Pending decision only; override requires reason."""
        if self.state != ApplicationState.PENDING_DECISION:
            raise DomainError(
                f"Application not in PendingDecision; state={self.state}",
                code="INVALID_STATE",
            )
        if override and not override_reason:
            raise DomainError("override_reason required when override=True", code="OVERRIDE_REASON_REQUIRED")

    def enforce_funding_approval_command(self, approved_amount_usd: float) -> None:
        """Gate for ApplicationApproved: state + BR4 + BR5."""
        if self.state != ApplicationState.APPROVED_PENDING_HUMAN:
            raise DomainError(
                f"Application not in ApprovedPendingHuman; state={self.state}",
                code="INVALID_STATE",
            )
        self.br4_enforce_compliance_cleared_for_funding()
        self.br5_enforce_approved_amount_within_agent_limit(approved_amount_usd)

    def enforce_final_decline_command(self) -> None:
        if self.state != ApplicationState.DECLINED_PENDING_HUMAN:
            raise DomainError(
                f"Application not in DeclinedPendingHuman; state={self.state}",
                code="INVALID_STATE",
            )

    # --- Fraud score (input guard on loan path) -------------------------------------

    @staticmethod
    def enforce_fraud_score_range(fraud_score: float) -> None:
        """Validated with fraud screening command on loan aggregate path."""
        if not 0.0 <= fraud_score <= 1.0:
            raise DomainError("fraud_score must be between 0.0 and 1.0", code="INVALID_FRAUD_SCORE")

    # --- Backwards-compatible aliases (delegates to BR*) -----------------------------

    def assert_awaiting_credit_analysis(self) -> None:
        self.br2_enforce_credit_analysis_eligibility()

    def assert_analysis_complete(self) -> None:
        self.br3_enforce_analyses_complete_for_decision()

    def assert_compliance_cleared(self) -> None:
        self.br4_enforce_compliance_cleared_for_funding()

    def assert_can_approve(self, approved_amount_usd: float) -> None:
        self.br4_enforce_compliance_cleared_for_funding()
        self.br5_enforce_approved_amount_within_agent_limit(approved_amount_usd)

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
        return self.br6_resolve_recommendation_with_confidence_floor(recommendation, confidence_score)

    def assert_contributing_agent_sessions(self, sessions: list[AgentSessionAggregate]) -> None:
        self.br6_enforce_contributing_sessions_have_analysis(sessions)
