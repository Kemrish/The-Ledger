from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from ..event_store import EventStore
from ..models.events import StoredEvent


class Projection(Protocol):
    name: str

    async def handles(self, event: StoredEvent) -> bool: ...
    async def apply(self, event: StoredEvent, store: EventStore) -> None: ...


@dataclass
class ProjectionLag:
    projection_name: str
    lag_events: int
    lag_ms: int


class ProjectionDaemon:
    """Async catch-up daemon for registered projections."""

    def __init__(self, store: EventStore, projections: list[Projection], max_retries: int = 3):
        self._store = store
        self._projections = {p.name: p for p in projections}
        self._max_retries = max_retries
        self._running = False
        # asyncpg connections are not concurrency-safe. External calls (like /health)
        # can run concurrently with daemon processing, so serialize DB usage.
        self._conn_lock = asyncio.Lock()

    async def run_forever(self, poll_interval_ms: int = 100) -> None:
        self._running = True
        while self._running:
            await self._process_batch()
            await asyncio.sleep(poll_interval_ms / 1000)

    def stop(self) -> None:
        self._running = False

    async def _projection_checkpoint(self, name: str) -> int:
        row = await self._store._conn.fetchrow(
            "SELECT last_position FROM projection_checkpoints WHERE projection_name = $1",
            name,
        )
        return int(row["last_position"]) if row else 0

    async def _set_projection_checkpoint(self, name: str, pos: int) -> None:
        await self._store._conn.execute(
            """
            INSERT INTO projection_checkpoints (projection_name, last_position, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (projection_name)
            DO UPDATE SET last_position = EXCLUDED.last_position, updated_at = NOW()
            """,
            name,
            pos,
        )

    async def _acquire_projection_lock(self, projection_name: str) -> bool:
        """
        Best-effort distributed coordination primitive.
        Uses PostgreSQL advisory lock so multiple daemon nodes do not process the
        same projection simultaneously.
        """
        row = await self._store._conn.fetchrow(
            "SELECT pg_try_advisory_lock(hashtext($1)) AS locked",
            projection_name,
        )
        return bool(row and row["locked"])

    async def _release_projection_lock(self, projection_name: str) -> None:
        await self._store._conn.execute(
            "SELECT pg_advisory_unlock(hashtext($1))",
            projection_name,
        )

    async def _process_projection(self, name: str, projection: Projection) -> None:
        async with self._conn_lock:
            locked = await self._acquire_projection_lock(name)
            if not locked:
                return
            try:
                checkpoint = await self._projection_checkpoint(name)
                async for event in self._store.load_all(from_global_position=checkpoint, batch_size=500):
                    if event.global_position <= checkpoint:
                        continue
                    if not await projection.handles(event):
                        await self._set_projection_checkpoint(name, event.global_position)
                        checkpoint = event.global_position
                        continue

                    attempt = 0
                    while True:
                        try:
                            await projection.apply(event, self._store)
                            await self._set_projection_checkpoint(name, event.global_position)
                            checkpoint = event.global_position
                            break
                        except Exception:
                            attempt += 1
                            if attempt > self._max_retries:
                                # Skip poison event after retries to keep daemon healthy.
                                await self._set_projection_checkpoint(name, event.global_position)
                                checkpoint = event.global_position
                                break
                            await asyncio.sleep(0.05 * attempt)
            finally:
                await self._release_projection_lock(name)

    async def _process_batch(self) -> None:
        if not self._projections:
            return
        for name, projection in self._projections.items():
            await self._process_projection(name, projection)

    async def get_lag(self, projection_name: str) -> ProjectionLag:
        async with self._conn_lock:
            last = await self._projection_checkpoint(projection_name)
            max_row = await self._store._conn.fetchrow(
                "SELECT COALESCE(MAX(global_position), 0) AS max_pos, MAX(recorded_at) AS max_time FROM events"
            )
            max_pos = int(max_row["max_pos"] or 0)
            max_time = max_row["max_time"]
            if max_time is None:
                return ProjectionLag(projection_name=projection_name, lag_events=0, lag_ms=0)
            lag_events = max(0, max_pos - last)
            now = datetime.now(timezone.utc)
            lag_ms = int((now - max_time).total_seconds() * 1000) if lag_events > 0 else 0
            return ProjectionLag(projection_name=projection_name, lag_events=lag_events, lag_ms=max(0, lag_ms))

    async def get_all_lags(self) -> list[ProjectionLag]:
        return [await self.get_lag(name) for name in self._projections]
