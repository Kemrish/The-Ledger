"""
AgentSession aggregate: Gas Town pattern — context must be loaded before any analysis event.

**State machine (SessionState)**

- **NO_CONTEXT** — stream empty or not yet loaded.
- **READY** — `AgentContextLoaded` has been applied; analysis events allowed.

Replay enforces ordering: `CreditAnalysisCompleted` / `FraudScreeningCompleted` before context raises
`DomainError` (Gas Town violation).
"""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from ..models.events import StoredEvent, DomainError

if TYPE_CHECKING:
    from ..event_store import EventStore


class SessionState(str, Enum):
    NO_CONTEXT = "NoContext"
    READY = "Ready"


class AgentSessionAggregate:
    """Reconstructed by replaying agent-{agent_id}-{session_id} stream."""

    def __init__(self, agent_id: str, session_id: str) -> None:
        self.agent_id = agent_id
        self.session_id = session_id
        self.version: int = 0
        self.session_state = SessionState.NO_CONTEXT
        self.context_loaded = False
        self.model_version: str | None = None
        self.application_ids_with_decision: set[str] = set()

    @classmethod
    async def load(cls, store: EventStore, agent_id: str, session_id: str) -> AgentSessionAggregate:
        stream_id = f"agent-{agent_id}-{session_id}"
        events = await store.load_stream(stream_id)
        agg = cls(agent_id=agent_id, session_id=session_id)
        for event in events:
            agg._apply(event)
        return agg

    @staticmethod
    def enforce_new_session_stream(stream_version: int, stream_id: str) -> None:
        """First event on an agent stream must be AgentContextLoaded; stream must be empty."""
        if stream_version != 0:
            raise DomainError(
                f"Agent session {stream_id} already exists",
                code="DUPLICATE_SESSION",
            )

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event)
        self.version = event.stream_position + 1

    def _on_AgentContextLoaded(self, event: StoredEvent) -> None:
        self.context_loaded = True
        self.session_state = SessionState.READY
        self.model_version = event.payload.get("model_version")

    def _on_CreditAnalysisCompleted(self, event: StoredEvent) -> None:
        if not self.context_loaded:
            raise DomainError(
                "Invalid stream: CreditAnalysisCompleted before AgentContextLoaded",
                code="GAS_TOWN_VIOLATION",
            )
        self.application_ids_with_decision.add(event.payload.get("application_id", ""))

    def _on_FraudScreeningCompleted(self, event: StoredEvent) -> None:
        if not self.context_loaded:
            raise DomainError(
                "Invalid stream: FraudScreeningCompleted before AgentContextLoaded",
                code="GAS_TOWN_VIOLATION",
            )
        self.application_ids_with_decision.add(event.payload.get("application_id", ""))

    def _on_PolicyEvaluationCompleted(self, event: StoredEvent) -> None:
        if not self.context_loaded:
            raise DomainError(
                "Invalid stream: PolicyEvaluationCompleted before AgentContextLoaded",
                code="GAS_TOWN_VIOLATION",
            )
        self.application_ids_with_decision.add(event.payload.get("application_id", ""))

    def assert_context_loaded(self) -> None:
        if not self.context_loaded or self.session_state != SessionState.READY:
            raise DomainError(
                "Agent session must have AgentContextLoaded before any decision event",
                code="CONTEXT_NOT_LOADED",
            )

    def assert_model_version_current(self, model_version: str) -> None:
        if self.model_version is not None and self.model_version != model_version:
            raise DomainError(
                f"Model version mismatch: session has {self.model_version}, command has {model_version}",
                code="MODEL_VERSION_MISMATCH",
            )

    def has_decision_for_application(self, application_id: str) -> bool:
        return application_id in self.application_ids_with_decision
