"""
Targeted domain tests: four aggregates, LoanApplication BR1–BR6, state machines, load/replay.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from src.aggregates.agent_session import AgentSessionAggregate, SessionState
from src.aggregates.audit_ledger import AuditLedgerAggregate, AuditLedgerState
from src.aggregates.compliance_record import ComplianceRecordAggregate
from src.aggregates.loan_application import ApplicationState, LoanApplicationAggregate
from src.commands.models import ComplianceCheckCommand
from src.models.events import DomainError, StoredEvent


def _se(
    stream_id: str,
    pos: int,
    event_type: str,
    payload: dict,
    gp: int = 1,
) -> StoredEvent:
    return StoredEvent(
        event_id=uuid4(),
        stream_id=stream_id,
        stream_position=pos,
        global_position=gp + pos,
        event_type=event_type,
        event_version=1,
        payload=payload,
        metadata={},
        recorded_at=datetime.now(timezone.utc),
    )


class TestLoanApplicationBusinessRules:
    def test_br1_unique_application(self):
        LoanApplicationAggregate.br1_enforce_unique_application(0, "new")
        with pytest.raises(DomainError) as e:
            LoanApplicationAggregate.br1_enforce_unique_application(1, "exists")
        assert e.value.code == "DUPLICATE_APPLICATION"

    def test_br2_credit_analysis_twice(self):
        app = LoanApplicationAggregate("a1")
        app._apply(
            _se(
                "loan-a1",
                0,
                "ApplicationSubmitted",
                {
                    "applicant_id": "u",
                    "requested_amount_usd": 1.0,
                    "loan_purpose": "x",
                    "submission_channel": "api",
                    "submitted_at": "2026-01-01T00:00:00Z",
                },
            )
        )
        app._apply(
            _se(
                "loan-a1",
                1,
                "CreditAnalysisCompleted",
                {
                    "application_id": "a1",
                    "agent_id": "c",
                    "session_id": "s",
                    "model_version": "v1",
                    "confidence_score": 0.9,
                    "risk_tier": "LOW",
                    "recommended_limit_usd": 100.0,
                    "analysis_duration_ms": 1,
                    "input_data_hash": "h",
                },
            )
        )
        with pytest.raises(DomainError) as e:
            app.br2_enforce_credit_analysis_eligibility()
        assert e.value.code == "DUPLICATE_CREDIT_ANALYSIS"

    def test_br3_analyses_before_decision(self):
        app = LoanApplicationAggregate("a1")
        app.credit_analysis_done = True
        app.fraud_screening_done = False
        with pytest.raises(DomainError) as e:
            app.br3_enforce_analyses_complete_for_decision()
        assert e.value.code == "ANALYSIS_INCOMPLETE"

    def test_br4_br5_funding(self):
        app = LoanApplicationAggregate("a1")
        app.state = ApplicationState.APPROVED_PENDING_HUMAN
        app.recommended_limit_usd = 1000.0
        app.compliance_cleared = False
        with pytest.raises(DomainError) as e:
            app.enforce_funding_approval_command(500.0)
        assert e.value.code == "COMPLIANCE_PENDING"

        app.compliance_cleared = True
        with pytest.raises(DomainError) as e:
            app.enforce_funding_approval_command(2000.0)
        assert e.value.code == "CREDIT_LIMIT_EXCEEDED"

    def test_br6_confidence_floor(self):
        app = LoanApplicationAggregate("a1")
        assert app.br6_resolve_recommendation_with_confidence_floor("APPROVE", 0.5) == "REFER"
        assert app.br6_resolve_recommendation_with_confidence_floor("APPROVE", 0.6) == "APPROVE"

    def test_state_machine_invalid_transition(self):
        app = LoanApplicationAggregate("a1")
        app.state = ApplicationState.FINAL_APPROVED
        with pytest.raises(DomainError) as e:
            app._transition(ApplicationState.PENDING_DECISION)
        assert e.value.code == "INVALID_STATE_TRANSITION"


class TestAgentSessionStateMachine:
    def test_replay_orders_context_first(self):
        agg = AgentSessionAggregate("c", "s")
        with pytest.raises(DomainError) as e:
            agg._apply(
                _se(
                    "agent-c-s",
                    0,
                    "CreditAnalysisCompleted",
                    {"application_id": "a", "agent_id": "c", "session_id": "s"},
                )
            )
        assert e.value.code == "GAS_TOWN_VIOLATION"
        assert agg.session_state == SessionState.NO_CONTEXT


class TestComplianceAggregate:
    def test_build_events_checklist_then_pass(self):
        comp = ComplianceRecordAggregate("a1")
        cmd = ComplianceCheckCommand(
            application_id="a1",
            regulation_set_version="2026",
            checks_required=["R1"],
            passed_rule_id="R1",
            passed_rule_version="1",
            passed_evidence_hash="e",
        )
        evs = comp.build_events_for_command(cmd)
        assert len(evs) == 2
        assert evs[0].__class__.__name__ == "ComplianceCheckRequested"
        assert evs[1].__class__.__name__ == "ComplianceRulePassed"


class TestAuditLedger:
    def test_replay_sealed(self):
        agg = AuditLedgerAggregate("loan", "x")
        assert agg.ledger_state == AuditLedgerState.EMPTY
        agg._apply(
            StoredEvent(
                event_id=uuid4(),
                stream_id="audit-loan-x",
                stream_position=0,
                global_position=1,
                event_type="AuditIntegrityCheckRun",
                event_version=1,
                payload={"integrity_hash": "abc", "entity_id": "x"},
                metadata={},
                recorded_at=datetime.now(timezone.utc),
            )
        )
        assert agg.ledger_state == AuditLedgerState.SEALED
        assert agg.last_integrity_hash == "abc"
