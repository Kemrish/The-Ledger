from __future__ import annotations

from ..event_store import EventStore
from ..models.events import StoredEvent


class AgentPerformanceProjection:
    name = "AgentPerformanceLedger"

    async def handles(self, event: StoredEvent) -> bool:
        return event.event_type in {"CreditAnalysisCompleted", "DecisionGenerated"}

    async def apply(self, event: StoredEvent, store: EventStore) -> None:
        p = event.payload
        agent_id = p.get("agent_id") or p.get("orchestrator_agent_id")
        if not agent_id:
            return
        model_version = p.get("model_version") or p.get("model_versions", {}).get(agent_id) or "unknown"

        is_analysis = 1 if event.event_type == "CreditAnalysisCompleted" else 0
        is_decision = 1 if event.event_type == "DecisionGenerated" else 0
        conf = p.get("confidence_score")
        dur = p.get("analysis_duration_ms")

        await store._conn.execute(
            """
            INSERT INTO agent_performance_projection (
              agent_id, model_version, analyses_completed, decisions_generated,
              avg_confidence_score, avg_duration_ms, approve_rate, decline_rate, refer_rate,
              human_override_rate, first_seen_at, last_seen_at, updated_at
            ) VALUES (
              $1, $2, $3, $4, $5, $6,
              0, 0, 0, 0, $7, $7, NOW()
            )
            ON CONFLICT (agent_id, model_version) DO UPDATE SET
              analyses_completed = agent_performance_projection.analyses_completed + EXCLUDED.analyses_completed,
              decisions_generated = agent_performance_projection.decisions_generated + EXCLUDED.decisions_generated,
              avg_confidence_score = CASE
                WHEN EXCLUDED.avg_confidence_score IS NULL THEN agent_performance_projection.avg_confidence_score
                WHEN agent_performance_projection.avg_confidence_score IS NULL THEN EXCLUDED.avg_confidence_score
                ELSE (agent_performance_projection.avg_confidence_score + EXCLUDED.avg_confidence_score) / 2.0
              END,
              avg_duration_ms = CASE
                WHEN EXCLUDED.avg_duration_ms IS NULL THEN agent_performance_projection.avg_duration_ms
                WHEN agent_performance_projection.avg_duration_ms IS NULL THEN EXCLUDED.avg_duration_ms
                ELSE (agent_performance_projection.avg_duration_ms + EXCLUDED.avg_duration_ms) / 2.0
              END,
              last_seen_at = EXCLUDED.last_seen_at,
              updated_at = NOW()
            """,
            agent_id,
            model_version,
            is_analysis,
            is_decision,
            conf,
            dur,
            event.recorded_at,
        )

        if event.event_type == "DecisionGenerated":
            rec = (p.get("recommendation") or "").upper()
            if rec in {"APPROVE", "DECLINE", "REFER"}:
                col = {"APPROVE": "approve_rate", "DECLINE": "decline_rate", "REFER": "refer_rate"}[rec]
                await store._conn.execute(
                    f"UPDATE agent_performance_projection SET {col} = COALESCE({col}, 0) + 1 WHERE agent_id = $1 AND model_version = $2",
                    agent_id,
                    model_version,
                )
