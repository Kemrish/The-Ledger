"""
ComplianceRecord aggregate: regulatory checks per application on stream compliance-{application_id}.

**State machine (ComplianceState)**

- **EMPTY** — no events yet.
- **CHECKLIST_DEFINED** — `ComplianceCheckRequested` received; required rule ids known.
- **EVALUATING** — at least one pass/fail recorded; may still be incomplete.
- **CLEAR** — all required rules have passed (subset of passed ⊇ required).
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

from ..models.events import (
    BaseEvent,
    ComplianceCheckRequested,
    ComplianceRuleFailed,
    ComplianceRulePassed,
    DomainError,
    StoredEvent,
)

if TYPE_CHECKING:
    from ..commands.models import ComplianceCheckCommand
    from ..event_store import EventStore


class ComplianceState(str, Enum):
    EMPTY = "Empty"
    CHECKLIST_DEFINED = "ChecklistDefined"
    EVALUATING = "Evaluating"
    CLEAR = "Clear"


class ComplianceRecordAggregate:
    """Reconstructed by replaying compliance-{application_id} stream."""

    def __init__(self, application_id: str) -> None:
        self.application_id = application_id
        self.version: int = 0
        self.state = ComplianceState.EMPTY
        self.checks_required: set[str] = set()
        self.passed_rules: set[str] = set()
        self.failed_rules: set[str] = set()
        self.regulation_set_version: str | None = None

    @classmethod
    async def load(cls, store: EventStore, application_id: str) -> ComplianceRecordAggregate:
        events = await store.load_stream(f"compliance-{application_id}")
        agg = cls(application_id=application_id)
        for event in events:
            agg._apply(event)
        return agg

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event)
        self.version = event.stream_position + 1
        self._recompute_state()

    def _recompute_state(self) -> None:
        if not self.checks_required:
            self.state = ComplianceState.EMPTY
            return
        if self.checks_required.issubset(self.passed_rules) and not (self.checks_required & self.failed_rules):
            self.state = ComplianceState.CLEAR
        elif self.passed_rules or self.failed_rules:
            self.state = ComplianceState.EVALUATING
        else:
            self.state = ComplianceState.CHECKLIST_DEFINED

    def _on_ComplianceCheckRequested(self, event: StoredEvent) -> None:
        self.regulation_set_version = event.payload.get("regulation_set_version")
        self.checks_required = set(event.payload.get("checks_required", []))

    def _on_ComplianceRulePassed(self, event: StoredEvent) -> None:
        self.passed_rules.add(event.payload.get("rule_id", ""))

    def _on_ComplianceRuleFailed(self, event: StoredEvent) -> None:
        self.failed_rules.add(event.payload.get("rule_id", ""))

    def all_required_checks_passed(self) -> bool:
        if not self.checks_required:
            return False
        return self.checks_required.issubset(self.passed_rules) and not (self.checks_required & self.failed_rules)

    def assert_can_clear(self) -> None:
        if not self.all_required_checks_passed():
            raise DomainError(
                "All mandatory compliance checks must pass before clearance",
                code="COMPLIANCE_INCOMPLETE",
            )

    def build_events_for_command(self, cmd: ComplianceCheckCommand) -> list[BaseEvent]:
        """
        Validate command against aggregate state; return domain events to append (empty = no-op).
        """
        events: list[BaseEvent] = []
        if not self.checks_required and cmd.checks_required:
            events.append(
                ComplianceCheckRequested(
                    application_id=cmd.application_id,
                    regulation_set_version=cmd.regulation_set_version,
                    checks_required=cmd.checks_required,
                )
            )
        ts = datetime.now(timezone.utc).isoformat()
        if cmd.passed_rule_id:
            events.append(
                ComplianceRulePassed(
                    application_id=cmd.application_id,
                    rule_id=cmd.passed_rule_id,
                    rule_version=cmd.passed_rule_version or "",
                    evaluation_timestamp=ts,
                    evidence_hash=cmd.passed_evidence_hash or "",
                )
            )
        if cmd.failed_rule_id:
            events.append(
                ComplianceRuleFailed(
                    application_id=cmd.application_id,
                    rule_id=cmd.failed_rule_id,
                    rule_version=cmd.failed_rule_version or "",
                    failure_reason=cmd.failure_reason or "",
                    remediation_required=cmd.remediation_required,
                )
            )
        return events
