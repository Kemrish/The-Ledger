"""
AuditLedger aggregate: integrity checkpoints on stream audit-{entity_type}-{entity_id}.

**State machine (AuditLedgerState)**

- **EMPTY** — no integrity runs yet.
- **SEALED** — at least one `AuditIntegrityCheckRun`; chain head is `last_integrity_hash`.

The store enforces append-only semantics; this aggregate replays seals for read-side validation.
"""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from ..models.events import StoredEvent

if TYPE_CHECKING:
    from ..event_store import EventStore


class AuditLedgerState(str, Enum):
    EMPTY = "Empty"
    SEALED = "Sealed"


class AuditLedgerAggregate:
    """Reconstructed by replaying audit-{entity_type}-{entity_id} stream."""

    def __init__(self, entity_type: str, entity_id: str) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.version: int = 0
        self.ledger_state = AuditLedgerState.EMPTY
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
        self.ledger_state = AuditLedgerState.SEALED

    def enforce_append_only_policy(self) -> None:
        """Audit streams are append-only; corrections are new events, not updates (enforced by EventStore)."""
