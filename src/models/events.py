"""
Event catalogue and store types for The Ledger.
Pydantic models for all event types, StoredEvent wrapper, StreamMetadata, and domain exceptions.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# --- Exceptions (typed for LLM/agent consumption; no domain logic in models) ---


class LedgerError(Exception):
    """Base for store- and domain-level errors that callers should handle explicitly."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.__class__.__name__,
            "message": str(self),
        }


class OptimisticConcurrencyError(LedgerError):
    """
    Raised when append expected_version does not match the stream row's current_version
    after SELECT ... FOR UPDATE (optimistic locking). actual_version is the observed
    version at commit time; retry by reloading the stream and re-applying commands.
    """

    def __init__(
        self,
        stream_id: str,
        expected_version: int,
        actual_version: int,
        message: str | None = None,
    ):
        self.stream_id = stream_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        self.suggested_action = "reload_stream_and_retry"
        super().__init__(
            message
            or f"Concurrency conflict: stream {stream_id} expected_version={expected_version} actual_version={actual_version}"
        )

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base.update(
            {
                "stream_id": self.stream_id,
                "expected_version": self.expected_version,
                "actual_version": self.actual_version,
                "suggested_action": self.suggested_action,
            }
        )
        return base


class DomainError(LedgerError):
    """Raised when a business rule is violated (state machine, invariants, preconditions)."""

    def __init__(self, message: str, code: str | None = None):
        self.code = code or "DOMAIN_VIOLATION"
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base["code"] = self.code
        return base


# --- Base event (for appending) and stored event (from store, with store metadata) ---


class BaseEvent(BaseModel):
    """Base for all domain events. Subclasses define event_type and payload shape."""

    model_config = ConfigDict(extra="allow")


class StoredEvent(BaseModel):
    """Event as loaded from the store: payload + store metadata. Upcasting applied at load time."""

    model_config = ConfigDict(extra="ignore")

    event_id: UUID
    stream_id: str
    stream_position: int
    global_position: int
    event_type: str
    event_version: int
    payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime

    def with_payload(self, payload: dict[str, Any], version: int | None = None) -> "StoredEvent":
        return StoredEvent(
            event_id=self.event_id,
            stream_id=self.stream_id,
            stream_position=self.stream_position,
            global_position=self.global_position,
            event_type=self.event_type,
            event_version=version if version is not None else self.event_version,
            payload=payload,
            metadata=self.metadata,
            recorded_at=self.recorded_at,
        )


class StreamMetadata(BaseModel):
    """Metadata for a stream (from event_streams table)."""

    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: datetime
    archived_at: datetime | None
    metadata: dict[str, Any] = Field(default_factory=dict)


# --- Event catalogue (payloads only; event_type from class name or explicit) ---


class ApplicationSubmitted(BaseEvent):
    """LoanApplication v1."""

    application_id: str
    applicant_id: str
    requested_amount_usd: float
    loan_purpose: str
    submission_channel: str
    submitted_at: str  # ISO datetime


class CreditAnalysisRequested(BaseEvent):
    """LoanApplication v1."""

    application_id: str
    assigned_agent_id: str
    requested_at: str
    priority: str | None = None


class CreditAnalysisCompleted(BaseEvent):
    """AgentSession v2. Tracks which agent completed analysis for which application."""

    application_id: str
    agent_id: str
    session_id: str
    model_version: str | None = None  # null after upcast when absent in historical v1 payloads
    confidence_score: float | None
    risk_tier: str
    recommended_limit_usd: float
    analysis_duration_ms: int
    input_data_hash: str
    regulatory_basis: str | None = None  # v2 field; null when unknown


class FraudScreeningCompleted(BaseEvent):
    """AgentSession v1."""

    application_id: str
    agent_id: str
    fraud_score: float  # 0.0–1.0
    anomaly_flags: list[str] = Field(default_factory=list)
    screening_model_version: str
    input_data_hash: str


class ComplianceCheckRequested(BaseEvent):
    """ComplianceRecord v1."""

    application_id: str
    regulation_set_version: str
    checks_required: list[str]


class ComplianceRulePassed(BaseEvent):
    """ComplianceRecord v1."""

    application_id: str
    rule_id: str
    rule_version: str
    evaluation_timestamp: str
    evidence_hash: str


class ComplianceRuleFailed(BaseEvent):
    """ComplianceRecord v1."""

    application_id: str
    rule_id: str
    rule_version: str
    failure_reason: str
    remediation_required: bool = False


class DecisionGenerated(BaseEvent):
    """LoanApplication v2. Orchestrator output."""

    application_id: str
    orchestrator_agent_id: str
    recommendation: str  # APPROVE | DECLINE | REFER
    confidence_score: float
    contributing_agent_sessions: list[str]
    decision_basis_summary: str
    model_versions: dict[str, str] | None = None  # null when unknown after upcast from v1


class HumanReviewCompleted(BaseEvent):
    """LoanApplication v1."""

    application_id: str
    reviewer_id: str
    override: bool
    final_decision: str
    override_reason: str | None = None


class ApplicationApproved(BaseEvent):
    """LoanApplication v1."""

    application_id: str
    approved_amount_usd: float
    interest_rate: float
    conditions: list[str] = Field(default_factory=list)
    approved_by: str  # human_id or "auto"
    effective_date: str


class ApplicationDeclined(BaseEvent):
    """LoanApplication v1."""

    application_id: str
    decline_reasons: list[str]
    declined_by: str
    adverse_action_notice_required: bool = False


class AgentContextLoaded(BaseEvent):
    """AgentSession v1. Gas Town: required before any decision event."""

    agent_id: str
    session_id: str
    context_source: str
    event_replay_from_position: int
    context_token_count: int
    model_version: str


class AuditIntegrityCheckRun(BaseEvent):
    """AuditLedger v1."""

    entity_id: str
    check_timestamp: str
    events_verified_count: int
    integrity_hash: str
    previous_hash: str | None = None


# --- Registry: event type string -> class (for serialization) ---

EVENT_TYPE_TO_CLASS: dict[str, type[BaseEvent]] = {
    "ApplicationSubmitted": ApplicationSubmitted,
    "CreditAnalysisRequested": CreditAnalysisRequested,
    "CreditAnalysisCompleted": CreditAnalysisCompleted,
    "FraudScreeningCompleted": FraudScreeningCompleted,
    "ComplianceCheckRequested": ComplianceCheckRequested,
    "ComplianceRulePassed": ComplianceRulePassed,
    "ComplianceRuleFailed": ComplianceRuleFailed,
    "DecisionGenerated": DecisionGenerated,
    "HumanReviewCompleted": HumanReviewCompleted,
    "ApplicationApproved": ApplicationApproved,
    "ApplicationDeclined": ApplicationDeclined,
    "AgentContextLoaded": AgentContextLoaded,
    "AuditIntegrityCheckRun": AuditIntegrityCheckRun,
}
