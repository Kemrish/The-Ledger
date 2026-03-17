"""
Event Store — append-only ledger with optimistic concurrency and outbox.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from uuid import UUID, uuid4

import asyncpg

from .models.events import (
    BaseEvent,
    StoredEvent,
    StreamMetadata,
    OptimisticConcurrencyError,
)


def _event_to_row(stream_id: str, stream_position: int, event: BaseEvent, event_version: int = 1) -> tuple[UUID, str, int, str, int, dict, dict]:
    """Produce (event_id, stream_id, stream_position, event_type, event_version, payload, metadata)."""
    event_id = uuid4()
    event_type = type(event).__name__
    payload = event.model_dump(mode="json")
    metadata: dict = {}
    return (event_id, stream_id, stream_position, event_type, event_version, payload, metadata)


class EventStore:
    """
    Async event store over PostgreSQL.
    Append with expected_version; load streams or global log; outbox written in same transaction.
    """

    def __init__(self, conn: asyncpg.Connection, outbox_destination: str = "default"):
        self._conn = conn
        self._outbox_destination = outbox_destination

    async def append(
        self,
        stream_id: str,
        events: list[BaseEvent],
        expected_version: int,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> int:
        """
        Atomically appends events to stream_id.
        expected_version: -1 = new stream (current_version must be 0); N = exact version required.
        Raises OptimisticConcurrencyError if stream version != expected_version.
        Writes to outbox in same transaction.
        Returns new stream version (after append).
        """
        if not events:
            return await self.stream_version(stream_id)

        async with self._conn.transaction():
            await self._conn.execute(
                """
                INSERT INTO event_streams (stream_id, aggregate_type, current_version, metadata)
                VALUES ($1, $2, 0, '{}'::jsonb)
                ON CONFLICT (stream_id) DO NOTHING
                """,
                stream_id,
                _aggregate_type_from_stream_id(stream_id),
            )
            row = await self._conn.fetchrow(
                "SELECT current_version FROM event_streams WHERE stream_id = $1 FOR UPDATE",
                stream_id,
            )
            if not row:
                raise OptimisticConcurrencyError(
                    stream_id=stream_id,
                    expected_version=expected_version,
                    actual_version=-1,
                )
            current = int(row["current_version"])

            # For new stream we allow expected_version in (-1, 0)
            if expected_version == -1:
                if current != 0:
                    raise OptimisticConcurrencyError(
                        stream_id=stream_id,
                        expected_version=expected_version,
                        actual_version=current,
                    )
            elif current != expected_version:
                raise OptimisticConcurrencyError(
                    stream_id=stream_id,
                    expected_version=expected_version,
                    actual_version=current,
                )

            new_version = current
            event_ids: list[UUID] = []
            for i, ev in enumerate(events):
                pos = current + i
                event_id, _, stream_position, event_type, event_version, payload, metadata = _event_to_row(
                    stream_id, pos, ev
                )
                if correlation_id:
                    metadata["correlation_id"] = correlation_id
                if causation_id:
                    metadata["causation_id"] = causation_id

                await self._conn.execute(
                    """
                    INSERT INTO events (event_id, stream_id, stream_position, event_type, event_version, payload, metadata)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    event_id,
                    stream_id,
                    stream_position,
                    event_type,
                    event_version,
                    json.dumps(payload) if isinstance(payload, dict) else payload,
                    json.dumps(metadata),
                )
                event_ids.append(event_id)
                new_version = pos + 1

            await self._conn.execute(
                "UPDATE event_streams SET current_version = $1 WHERE stream_id = $2",
                new_version,
                stream_id,
            )

            for event_id in event_ids:
                # Outbox: one row per event for reliable publish (same transaction)
                payload_row = await self._conn.fetchrow(
                    "SELECT payload, metadata FROM events WHERE event_id = $1", event_id
                )
                await self._conn.execute(
                    """
                    INSERT INTO outbox (event_id, destination, payload)
                    VALUES ($1, $2, $3)
                    """,
                    event_id,
                    self._outbox_destination,
                    json.dumps({"event_id": str(event_id), "payload": payload_row["payload"], "metadata": payload_row["metadata"]}),
                )

        return new_version

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[StoredEvent]:
        """Load events in stream order, from_position inclusive. to_position inclusive if set. Upcasting applied by caller (store returns raw)."""
        q = """
            SELECT event_id, stream_id, stream_position, global_position, event_type, event_version,
                   payload, metadata, recorded_at
            FROM events
            WHERE stream_id = $1 AND stream_position >= $2
            """
        params: list = [stream_id, from_position]
        if to_position is not None:
            q += " AND stream_position <= $3"
            params.append(to_position)
        q += " ORDER BY stream_position ASC"

        rows = await self._conn.fetch(q, *params)
        return [_row_to_stored(r) for r in rows]

    async def load_all(
        self,
        from_global_position: int = 0,
        event_types: list[str] | None = None,
        batch_size: int = 500,
    ) -> AsyncIterator[StoredEvent]:
        """Async generator over events from global position. Optional event_types filter."""
        pos = from_global_position
        while True:
            q = """
                SELECT event_id, stream_id, stream_position, global_position, event_type, event_version,
                       payload, metadata, recorded_at
                FROM events
                WHERE global_position > $1
                """
            params: list = [pos]
            if event_types:
                q += " AND event_type = ANY($2::text[])"
                params.append(event_types)
            q += " ORDER BY global_position ASC LIMIT $" + str(len(params) + 1)
            params.append(batch_size)

            rows = await self._conn.fetch(q, *params)
            if not rows:
                break
            for r in rows:
                yield _row_to_stored(r)
                pos = r["global_position"]
            if len(rows) < batch_size:
                break

    async def stream_version(self, stream_id: str) -> int:
        """Current version of stream (number of events). 0 if stream does not exist."""
        row = await self._conn.fetchrow(
            "SELECT current_version FROM event_streams WHERE stream_id = $1",
            stream_id,
        )
        return int(row["current_version"]) if row else 0

    async def archive_stream(self, stream_id: str) -> None:
        """Mark stream as archived (archived_at set). Events remain; new appends typically rejected by policy."""
        await self._conn.execute(
            "UPDATE event_streams SET archived_at = NOW() WHERE stream_id = $1",
            stream_id,
        )

    async def get_stream_metadata(self, stream_id: str) -> StreamMetadata | None:
        """Full metadata for stream, or None if not found."""
        row = await self._conn.fetchrow(
            "SELECT stream_id, aggregate_type, current_version, created_at, archived_at, metadata FROM event_streams WHERE stream_id = $1",
            stream_id,
        )
        if not row:
            return None
        return StreamMetadata(
            stream_id=row["stream_id"],
            aggregate_type=row["aggregate_type"],
            current_version=row["current_version"],
            created_at=row["created_at"],
            archived_at=row["archived_at"],
            metadata=row["metadata"] or {},
        )


def _aggregate_type_from_stream_id(stream_id: str) -> str:
    if stream_id.startswith("loan-"):
        return "LoanApplication"
    if stream_id.startswith("agent-"):
        return "AgentSession"
    if stream_id.startswith("compliance-"):
        return "ComplianceRecord"
    if stream_id.startswith("audit-"):
        return "AuditLedger"
    return "Unknown"


def _row_to_stored(r: asyncpg.Record) -> StoredEvent:
    payload = r["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    metadata = r["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata) if metadata else {}
    return StoredEvent(
        event_id=r["event_id"],
        stream_id=r["stream_id"],
        stream_position=r["stream_position"],
        global_position=r["global_position"],
        event_type=r["event_type"],
        event_version=r["event_version"],
        payload=payload,
        metadata=metadata or {},
        recorded_at=r["recorded_at"],
    )
