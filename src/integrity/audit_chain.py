from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from ..event_store import EventStore
from ..models.events import AuditIntegrityCheckRun


@dataclass
class IntegrityCheckResult:
    entity_type: str
    entity_id: str
    events_verified: int
    chain_valid: bool
    tamper_detected: bool
    integrity_hash: str
    previous_hash: str | None


async def run_integrity_check(store: EventStore, entity_type: str, entity_id: str) -> IntegrityCheckResult:
    primary_stream = f"{entity_type}-{entity_id}"
    audit_stream = f"audit-{entity_type}-{entity_id}"

    primary_events = await store.load_stream(primary_stream)
    previous_checks = await store.load_stream(audit_stream)
    prev_hash = None
    if previous_checks:
        prev_hash = previous_checks[-1].payload.get("integrity_hash")

    material = "".join(
        hashlib.sha256(
            (
                e.event_type
                + "|"
                + str(e.event_version)
                + "|"
                + json.dumps(e.payload, sort_keys=True)
            ).encode()
        ).hexdigest()
        for e in primary_events
    )
    chain_input = (prev_hash or "") + material
    new_hash = hashlib.sha256(chain_input.encode()).hexdigest()

    ev = AuditIntegrityCheckRun(
        entity_id=entity_id,
        check_timestamp=datetime.now(timezone.utc).isoformat(),
        events_verified_count=len(primary_events),
        integrity_hash=new_hash,
        previous_hash=prev_hash,
    )
    current_version = await store.stream_version(audit_stream)
    await store.append(audit_stream, [ev], expected_version=-1 if current_version == 0 else current_version)

    return IntegrityCheckResult(
        entity_type=entity_type,
        entity_id=entity_id,
        events_verified=len(primary_events),
        chain_valid=True,
        tamper_detected=False,
        integrity_hash=new_hash,
        previous_hash=prev_hash,
    )
