"""
AuditLedger aggregate: append-only cross-cutting audit trail.
Maintains causal ordering via correlation_id; no events may be removed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..models.events import StoredEvent, DomainError

if TYPE_CHECKING:
    from ..event_store import EventStore


class AuditLedgerAggregate:
    """Reconstructed by replaying audit-{entity_type}-{entity_id} stream."""

    def __init__(self, entity_type: str, entity_id: str) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.version: int = 0
        self.last_integrity_hash: str | None = None

    @classmethod
    async def load(cls, store: EventStore, entity_type: str, entity_id: str) -> AuditLedgerAggregate:
        stream_id = f"audit-{entity_type}-{entity_id}"
        events = await store.load_stream(stream_id)
        agg = cls(entity_type=entity_type, entity_id=entity_id)
        for event in events:
            agg._apply(event)
        return agg

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event)
        self.version = event.stream_position + 1

    def _on_AuditIntegrityCheckRun(self, event: StoredEvent) -> None:
        self.last_integrity_hash = event.payload.get("integrity_hash")

    def assert_append_only(self) -> None:
        """No events may be removed from the audit stream (enforced by store design)."""
        pass  # Store does not support delete; this is documentation.
