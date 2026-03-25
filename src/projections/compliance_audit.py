from __future__ import annotations

from datetime import datetime

from ..event_store import EventStore
from ..models.events import StoredEvent


class ComplianceAuditProjection:
    name = "ComplianceAuditView"

    async def handles(self, event: StoredEvent) -> bool:
        return event.stream_id.startswith("compliance-")

    async def apply(self, event: StoredEvent, store: EventStore) -> None:
        app_id = event.payload.get("application_id") or event.stream_id.replace("compliance-", "")

        prev = await store._conn.fetchrow(
            """
            SELECT regulation_set_version, checks_required, passed_rules, failed_rules, status
            FROM compliance_audit_projection
            WHERE application_id = $1
            ORDER BY as_of_event_position DESC
            LIMIT 1
            """,
            app_id,
        )

        required = list(prev["checks_required"]) if prev else []
        passed = list(prev["passed_rules"]) if prev else []
        failed = list(prev["failed_rules"]) if prev else []
        regulation = prev["regulation_set_version"] if prev else None

        if event.event_type == "ComplianceCheckRequested":
            required = list(event.payload.get("checks_required", []))
            regulation = event.payload.get("regulation_set_version")
        elif event.event_type == "ComplianceRulePassed":
            rid = event.payload.get("rule_id")
            if rid and rid not in passed:
                passed.append(rid)
        elif event.event_type == "ComplianceRuleFailed":
            rid = event.payload.get("rule_id")
            if rid and rid not in failed:
                failed.append(rid)

        status = "PENDING"
        if required:
            if any(r in failed for r in required):
                status = "FAILED"
            elif set(required).issubset(set(passed)):
                status = "CLEAR"

        await store._conn.execute(
            """
            INSERT INTO compliance_audit_projection (
              application_id, as_of_event_position, as_of_recorded_at,
              regulation_set_version, checks_required, passed_rules, failed_rules,
              status, latest_event_type, updated_at
            ) VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8, $9, NOW())
            """,
            app_id,
            event.global_position,
            event.recorded_at,
            regulation,
            required,
            passed,
            failed,
            status,
            event.event_type,
        )

    async def get_current_compliance(self, store: EventStore, application_id: str):
        return await store._conn.fetchrow(
            """
            SELECT * FROM compliance_audit_projection
            WHERE application_id = $1
            ORDER BY as_of_event_position DESC
            LIMIT 1
            """,
            application_id,
        )

    async def get_compliance_at(self, store: EventStore, application_id: str, as_of: datetime):
        return await store._conn.fetchrow(
            """
            SELECT * FROM compliance_audit_projection
            WHERE application_id = $1 AND as_of_recorded_at <= $2
            ORDER BY as_of_event_position DESC
            LIMIT 1
            """,
            application_id,
            as_of,
        )

    async def rebuild_from_scratch(self, store: EventStore) -> None:
        await store._conn.execute("TRUNCATE TABLE compliance_audit_projection")
