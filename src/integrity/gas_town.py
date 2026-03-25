from __future__ import annotations

from dataclasses import dataclass

from ..event_store import EventStore


@dataclass
class AgentContext:
    context_text: str
    last_event_position: int
    pending_work: list[str]
    session_health_status: str


async def reconstruct_agent_context(
    store: EventStore,
    agent_id: str,
    session_id: str,
    token_budget: int = 8000,
) -> AgentContext:
    stream_id = f"agent-{agent_id}-{session_id}"
    events = await store.load_stream(stream_id)
    if not events:
        return AgentContext(
            context_text="No prior session events found.",
            last_event_position=0,
            pending_work=["initialize_session"],
            session_health_status="EMPTY",
        )

    last = events[-1]
    last_three = events[-3:]
    pending = []
    health = "HEALTHY"

    if "Failed" in last.event_type or last.event_type.endswith("Requested"):
        health = "NEEDS_RECONCILIATION"
        pending.append("reconcile_partial_state")

    summary_lines = [
        f"Session {stream_id} replayed: {len(events)} events.",
        f"Last event: {last.event_type} at position {last.stream_position}.",
    ]
    summary_lines.append("Recent events:")
    for e in last_three:
        summary_lines.append(f"- {e.stream_position}: {e.event_type}")

    text = "\n".join(summary_lines)
    if len(text) > token_budget * 4:
        text = text[: token_budget * 4]

    return AgentContext(
        context_text=text,
        last_event_position=last.stream_position,
        pending_work=pending,
        session_health_status=health,
    )
