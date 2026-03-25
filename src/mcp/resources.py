"""
MCP resources (query side of CQRS).

Read models come from projection tables — not full aggregate replay — except where noted:
  - `audit_trail_resource` / `session_resource` intentionally load streams for audit replay and
    Gas Town session reconstruction (per spec “justified exception”).
"""
from __future__ import annotations

from datetime import datetime


def _row_to_dict(row):
    if row is None:
        return None
    if hasattr(row, "items"):
        return dict(row.items())
    return row


async def application_resource(store, application_id: str):
    row = await store._conn.fetchrow("SELECT * FROM application_summary_projection WHERE application_id = $1", application_id)
    return _row_to_dict(row)


async def compliance_resource(store, application_id: str, as_of: str | None = None):
    if as_of:
        ts = datetime.fromisoformat(as_of)
        row = await store._conn.fetchrow(
            """SELECT * FROM compliance_audit_projection
               WHERE application_id = $1 AND as_of_recorded_at <= $2
               ORDER BY as_of_event_position DESC LIMIT 1""",
            application_id,
            ts,
        )
        return _row_to_dict(row)
    row = await store._conn.fetchrow(
        "SELECT * FROM compliance_audit_projection WHERE application_id = $1 ORDER BY as_of_event_position DESC LIMIT 1",
        application_id,
    )
    return _row_to_dict(row)


async def audit_trail_resource(store, entity_type: str, entity_id: str):
    stream_id = f"audit-{entity_type}-{entity_id}"
    return await store.load_stream(stream_id)


async def agent_performance_resource(store, agent_id: str):
    rows = await store._conn.fetch(
        "SELECT * FROM agent_performance_projection WHERE agent_id = $1 ORDER BY updated_at DESC",
        agent_id,
    )
    return [_row_to_dict(r) for r in rows]


async def session_resource(store, agent_id: str, session_id: str):
    return await store.load_stream(f"agent-{agent_id}-{session_id}")


async def ledger_health_resource(daemon):
    lags = await daemon.get_all_lags()
    return [{"projection": x.projection_name, "lag_events": x.lag_events, "lag_ms": x.lag_ms} for x in lags]
