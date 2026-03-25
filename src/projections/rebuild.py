"""
Rebuild all CQRS read models from the event store (truncate + reset checkpoints + replay).

Run the projection daemon after calling `reset_projection_checkpoints` so it catches up from global_position 0,
or invoke each projection's `apply` in a custom replayer for tests.
"""
from __future__ import annotations

from ..event_store import EventStore
from .application_summary import ApplicationSummaryProjection
from .agent_performance import AgentPerformanceProjection
from .compliance_audit import ComplianceAuditProjection


async def truncate_all_projection_tables(store: EventStore) -> None:
    """Clear all projection tables (destructive)."""
    await store._conn.execute("TRUNCATE TABLE application_summary_projection")
    await store._conn.execute("TRUNCATE TABLE agent_performance_projection")
    await store._conn.execute("TRUNCATE TABLE compliance_audit_projection")


async def reset_projection_checkpoints(store: EventStore) -> None:
    """Set all projector checkpoints to 0 so the daemon replays from the start."""
    for name in ("ApplicationSummary", "AgentPerformanceLedger", "ComplianceAuditView"):
        await store._conn.execute(
            """
            INSERT INTO projection_checkpoints (projection_name, last_position, updated_at)
            VALUES ($1, 0, NOW())
            ON CONFLICT (projection_name)
            DO UPDATE SET last_position = 0, updated_at = NOW()
            """,
            name,
        )


async def rebuild_projections_full(
    store: EventStore,
    *,
    reset_checkpoints: bool = True,
) -> None:
    """
    Truncate read models and optionally reset checkpoints.
    Next step: run `ProjectionDaemon` until lag is zero, or replay events manually in tests.
    """
    p1 = ApplicationSummaryProjection()
    p2 = AgentPerformanceProjection()
    p3 = ComplianceAuditProjection()
    await p1.rebuild_from_scratch(store)
    await p2.rebuild_from_scratch(store)
    await p3.rebuild_from_scratch(store)
    if reset_checkpoints:
        await reset_projection_checkpoints(store)


async def rebuild_projections_from_scratch(store: EventStore) -> None:
    """
    Truncate read models, reset checkpoints, and run the projection daemon until lag is zero.
    """
    await rebuild_projections_full(store, reset_checkpoints=True)
    from .daemon import ProjectionDaemon

    projections = [
        ApplicationSummaryProjection(),
        AgentPerformanceProjection(),
        ComplianceAuditProjection(),
    ]
    daemon = ProjectionDaemon(store, projections)
    for _ in range(10_000):
        await daemon._process_batch()
        lags = await daemon.get_all_lags()
        if all(x.lag_events == 0 for x in lags):
            break
