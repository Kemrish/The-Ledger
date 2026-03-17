"""Command models for Phase 2 domain logic."""
from datetime import datetime, timezone
from pydantic import BaseModel, Field


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SubmitApplicationCommand(BaseModel):
    application_id: str
    applicant_id: str
    requested_amount_usd: float
    loan_purpose: str
    submission_channel: str = "api"
    submitted_at: str | None = None  # default now

    def submitted_at_value(self) -> str:
        return self.submitted_at or _utc_now_iso()


class CreditAnalysisCompletedCommand(BaseModel):
    application_id: str
    agent_id: str
    session_id: str
    model_version: str
    confidence_score: float | None = None
    risk_tier: str
    recommended_limit_usd: float
    duration_ms: int = 0
    input_data: dict | None = None
    correlation_id: str | None = None
    causation_id: str | None = None


class FraudScreeningCompletedCommand(BaseModel):
    application_id: str
    agent_id: str
    session_id: str
    fraud_score: float  # 0.0–1.0
    anomaly_flags: list[str] = Field(default_factory=list)
    screening_model_version: str
    input_data_hash: str = ""
    correlation_id: str | None = None
    causation_id: str | None = None


class ComplianceCheckCommand(BaseModel):
    application_id: str
    regulation_set_version: str
    checks_required: list[str]
    passed_rule_id: str | None = None
    passed_rule_version: str | None = None
    passed_evidence_hash: str | None = None
    failed_rule_id: str | None = None
    failed_rule_version: str | None = None
    failure_reason: str | None = None
    remediation_required: bool = False
    correlation_id: str | None = None
    causation_id: str | None = None


class GenerateDecisionCommand(BaseModel):
    application_id: str
    orchestrator_agent_id: str
    session_id: str
    recommendation: str  # APPROVE | DECLINE | REFER
    confidence_score: float
    contributing_agent_sessions: list[str]  # stream IDs that have decision events for this application
    decision_basis_summary: str
    model_versions: dict[str, str] = Field(default_factory=dict)
    correlation_id: str | None = None
    causation_id: str | None = None


class HumanReviewCompletedCommand(BaseModel):
    application_id: str
    reviewer_id: str
    override: bool
    final_decision: str  # APPROVE | DECLINE
    override_reason: str | None = None


class StartAgentSessionCommand(BaseModel):
    agent_id: str
    session_id: str
    context_source: str
    event_replay_from_position: int = 0
    context_token_count: int = 0
    model_version: str
