from __future__ import annotations

from collections.abc import Callable

from ..models.events import StoredEvent


class UpcasterRegistry:
    def __init__(self):
        self._upcasters: dict[tuple[str, int], Callable[[dict], dict]] = {}

    def register(self, event_type: str, from_version: int):
        def decorator(fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
            self._upcasters[(event_type, from_version)] = fn
            return fn

        return decorator

    def upcast(self, event: StoredEvent) -> StoredEvent:
        current = event
        v = event.event_version
        while (event.event_type, v) in self._upcasters:
            fn = self._upcasters[(event.event_type, v)]
            new_payload = fn(current.payload)
            current = current.with_payload(new_payload, version=v + 1)
            v += 1
        return current
