from __future__ import annotations

import json

from ..event_store import EventStore
from ..models.events import StoredEvent


class ApplicationSummaryProjection:
    name = "ApplicationSummary"

    async def handles(self, event: StoredEvent) -> bool:
        return event.stream_id.startswith("loan-")

    async def apply(self, event: StoredEvent, store: EventStore) -> None:
        p = event.payload
        app_id = p.get("application_id") or event.stream_id.replace("loan-", "")

        state_map = {
            "ApplicationSubmitted": "Submitted",
            "CreditAnalysisCompleted": "AnalysisComplete",
            "FraudScreeningCompleted": "AnalysisComplete",
            "DecisionGenerated": "PendingDecision",
            "HumanReviewCompleted": "Reviewed",
            "ApplicationApproved": "FinalApproved",
            "ApplicationDeclined": "FinalDeclined",
        }
        # Important: the ApplicationSummary projection should only update on
        # domain lifecycle transitions; unrelated loan-stream events must not
        # overwrite state as "Unknown".
        if event.event_type not in state_map:
            return
        state = state_map[event.event_type]

        await store._conn.execute(
            """
            INSERT INTO application_summary_projection (
              application_id, state, applicant_id, requested_amount_usd, approved_amount_usd,
              risk_tier, fraud_score, compliance_status, decision, agent_sessions_completed,
              last_event_type, last_event_at, human_reviewer_id, final_decision_at, updated_at
            ) VALUES (
              $1, $2, $3, $4, $5,
              $6, $7, $8, $9, $10::jsonb,
              $11, $12, $13, $14, NOW()
            )
            ON CONFLICT (application_id) DO UPDATE SET
              state = COALESCE(EXCLUDED.state, application_summary_projection.state),
              applicant_id = COALESCE(EXCLUDED.applicant_id, application_summary_projection.applicant_id),
              requested_amount_usd = COALESCE(EXCLUDED.requested_amount_usd, application_summary_projection.requested_amount_usd),
              approved_amount_usd = COALESCE(EXCLUDED.approved_amount_usd, application_summary_projection.approved_amount_usd),
              risk_tier = COALESCE(EXCLUDED.risk_tier, application_summary_projection.risk_tier),
              fraud_score = COALESCE(EXCLUDED.fraud_score, application_summary_projection.fraud_score),
              decision = COALESCE(EXCLUDED.decision, application_summary_projection.decision),
              last_event_type = EXCLUDED.last_event_type,
              last_event_at = EXCLUDED.last_event_at,
              human_reviewer_id = COALESCE(EXCLUDED.human_reviewer_id, application_summary_projection.human_reviewer_id),
              final_decision_at = COALESCE(EXCLUDED.final_decision_at, application_summary_projection.final_decision_at),
              updated_at = NOW()
            """,
            app_id,
            state,
            p.get("applicant_id"),
            p.get("requested_amount_usd"),
            p.get("approved_amount_usd"),
            p.get("risk_tier"),
            p.get("fraud_score"),
            p.get("compliance_status"),
            p.get("recommendation") or p.get("final_decision"),
            json.dumps([]),
            event.event_type,
            event.recorded_at,
            p.get("reviewer_id"),
            event.recorded_at if event.event_type in ("ApplicationApproved", "ApplicationDeclined") else None,
        )

    async def rebuild_from_scratch(self, store: EventStore) -> None:
        await store._conn.execute("TRUNCATE TABLE application_summary_projection")
