from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from ..event_store import EventStore
from ..models.events import AuditIntegrityCheckRun, StoredEvent


def _compute_chain_hash(prev_hash: str | None, events: list[StoredEvent]) -> str:
    """Deterministic hash over ordered events, chained with the previous audit hash (hex string or empty)."""
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
        for e in events
    )
    chain_input = (prev_hash or "") + material
    return hashlib.sha256(chain_input.encode()).hexdigest()


def _verify_stored_checkpoints(
    primary_events: list[StoredEvent],
    audit_events: list[StoredEvent],
    entity_type: str,
    entity_id: str,
) -> IntegrityCheckResult:
    prev_hash: str | None = None
    for audit_stored in audit_events:
        if audit_stored.event_type != "AuditIntegrityCheckRun":
            continue
        p = audit_stored.payload
        n = int(p.get("events_verified_count", 0))
        if n < 0 or n > len(primary_events):
            return IntegrityCheckResult(
                entity_type=entity_type,
                entity_id=entity_id,
                events_verified=len(primary_events),
                chain_valid=False,
                tamper_detected=True,
                integrity_hash="",
                previous_hash=prev_hash,
            )
        slice_events = primary_events[:n]
        expected = _compute_chain_hash(p.get("previous_hash"), slice_events)
        if expected != p.get("integrity_hash"):
            return IntegrityCheckResult(
                entity_type=entity_type,
                entity_id=entity_id,
                events_verified=len(primary_events),
                chain_valid=False,
                tamper_detected=True,
                integrity_hash=str(p.get("integrity_hash", "")),
                previous_hash=prev_hash,
            )
        prev_hash = p.get("integrity_hash")

    latest = _compute_chain_hash(prev_hash, primary_events)
    return IntegrityCheckResult(
        entity_type=entity_type,
        entity_id=entity_id,
        events_verified=len(primary_events),
        chain_valid=True,
        tamper_detected=False,
        integrity_hash=latest,
        previous_hash=prev_hash,
    )


@dataclass
class IntegrityCheckResult:
    entity_type: str
    entity_id: str
    events_verified: int
    chain_valid: bool
    tamper_detected: bool
    integrity_hash: str
    previous_hash: str | None


async def verify_audit_chain(
    store: EventStore,
    entity_type: str,
    entity_id: str,
) -> IntegrityCheckResult:
    """
    Recompute and verify every sealed checkpoint in the audit stream against the primary stream.
    Does not append a new checkpoint.
    """
    primary_stream = f"{entity_type}-{entity_id}"
    audit_stream = f"audit-{entity_type}-{entity_id}"
    primary_events = await store.load_stream(primary_stream)
    audit_events = await store.load_stream(audit_stream)
    return _verify_stored_checkpoints(primary_events, audit_events, entity_type, entity_id)


async def run_integrity_check(store: EventStore, entity_type: str, entity_id: str) -> IntegrityCheckResult:
    """
    Verify existing audit checkpoints against the primary stream, then append a new AuditIntegrityCheckRun
    sealing the current primary history. If verification fails, no new row is appended.
    """
    primary_stream = f"{entity_type}-{entity_id}"
    audit_stream = f"audit-{entity_type}-{entity_id}"

    primary_events = await store.load_stream(primary_stream)
    audit_events = await store.load_stream(audit_stream)

    verified = _verify_stored_checkpoints(primary_events, audit_events, entity_type, entity_id)
    if not verified.chain_valid:
        return verified

    new_hash = verified.integrity_hash
    prev_hash = verified.previous_hash

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
