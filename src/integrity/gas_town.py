from __future__ import annotations

import json
from dataclasses import dataclass

from ..event_store import EventStore
from ..models.events import StoredEvent


@dataclass
class AgentContext:
    context_text: str
    last_event_position: int
    pending_work: list[str]
    session_health_status: str


def _event_type_suggests_incomplete(last: StoredEvent) -> bool:
    """Heuristic: orchestration/request events without a completion pair."""
    t = last.event_type
    if "Failed" in t:
        return True
    if t.endswith("Requested") or t.endswith("Started"):
        return True
    return False


async def reconstruct_agent_context(
    store: EventStore,
    agent_id: str,
    session_id: str,
    token_budget: int = 8000,
) -> AgentContext:
    """
    Replay the AgentSession stream and rebuild context after a crash (Gas Town).

    - Older events (all but the last 3) are summarized as compact lines.
    - The last 3 events are included verbatim (full payload JSON) for audit/debug.
    - NEEDS_RECONCILIATION: missing first AgentContextLoaded, incomplete last step,
      or last event looks like a partial/failed operation.
    """
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
    pending: list[str] = []
    health = "HEALTHY"

    if events[0].event_type != "AgentContextLoaded":
        health = "NEEDS_RECONCILIATION"
        pending.append("replay_missing_context_loaded: Gas Town requires AgentContextLoaded first")

    if _event_type_suggests_incomplete(last):
        health = "NEEDS_RECONCILIATION"
        pending.append("reconcile_partial_state")

    older = events[:-3] if len(events) > 3 else []
    last_three = events[-3:]

    lines: list[str] = [
        f"Session {stream_id} replayed: {len(events)} events.",
        f"Last event: {last.event_type} at position {last.stream_position}.",
    ]
    if older:
        lines.append("Earlier events (summary):")
        for e in older:
            lines.append(f"  pos {e.stream_position}: {e.event_type}")
    lines.append("Last 3 events (verbatim payloads):")
    for e in last_three:
        blob = {
            "stream_position": e.stream_position,
            "event_type": e.event_type,
            "event_version": e.event_version,
            "payload": e.payload,
        }
        lines.append(json.dumps(blob, default=str))

    text = "\n".join(lines)
    # Rough char budget; keep verbatim tail if trimming needed.
    max_chars = max(4000, token_budget * 4)
    if len(text) > max_chars:
        tail = "\n".join(lines[-4:])  # keep header-ish + last verbatim block
        head = "\n".join(lines[: max(2, len(lines) - 4)])
        text = (head[: max_chars // 2] + "\n...[truncated]...\n" + tail[-(max_chars // 2) :])[:max_chars]

    return AgentContext(
        context_text=text,
        last_event_position=last.stream_position,
        pending_work=pending,
        session_health_status=health,
    )
