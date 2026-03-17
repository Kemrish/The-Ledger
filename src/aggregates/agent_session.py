"""
AgentSession aggregate: Gas Town pattern — context must be loaded before any decision event.
Tracks model version and session actions.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..models.events import StoredEvent, DomainError

if TYPE_CHECKING:
    from ..event_store import EventStore


class AgentSessionAggregate:
    """Reconstructed by replaying agent-{agent_id}-{session_id} stream."""

    def __init__(self, agent_id: str, session_id: str) -> None:
        self.agent_id = agent_id
        self.session_id = session_id
        self.version: int = 0
        self.context_loaded = False
        self.model_version: str | None = None
        self.application_ids_with_decision: set[str] = set()  # application_id for which we have a decision event

    @classmethod
    async def load(cls, store: EventStore, agent_id: str, session_id: str) -> AgentSessionAggregate:
        stream_id = f"agent-{agent_id}-{session_id}"
        events = await store.load_stream(stream_id)
        agg = cls(agent_id=agent_id, session_id=session_id)
        for event in events:
            agg._apply(event)
        return agg

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event)
        self.version = event.stream_position + 1

    def _on_AgentContextLoaded(self, event: StoredEvent) -> None:
        self.context_loaded = True
        self.model_version = event.payload.get("model_version")

    def _on_CreditAnalysisCompleted(self, event: StoredEvent) -> None:
        # Decision-type event: must have had context loaded
        self.application_ids_with_decision.add(event.payload.get("application_id", ""))

    def _on_FraudScreeningCompleted(self, event: StoredEvent) -> None:
        self.application_ids_with_decision.add(event.payload.get("application_id", ""))

    def assert_context_loaded(self) -> None:
        """Gas Town: no decision event may be appended without AgentContextLoaded first."""
        if not self.context_loaded:
            raise DomainError(
                "Agent session must have AgentContextLoaded before any decision event",
                code="CONTEXT_NOT_LOADED",
            )

    def assert_model_version_current(self, model_version: str) -> None:
        """Optional: enforce that the command uses the same model version as the loaded context."""
        if self.model_version is not None and self.model_version != model_version:
            raise DomainError(
                f"Model version mismatch: session has {self.model_version}, command has {model_version}",
                code="MODEL_VERSION_MISMATCH",
            )

    def has_decision_for_application(self, application_id: str) -> bool:
        return application_id in self.application_ids_with_decision
