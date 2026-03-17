"""
ComplianceRecord aggregate: regulatory checks and verdicts per application.
Separate stream to avoid false concurrency with loan lifecycle writes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..models.events import StoredEvent, DomainError

if TYPE_CHECKING:
    from ..event_store import EventStore


class ComplianceRecordAggregate:
    """Reconstructed by replaying compliance-{application_id} stream."""

    def __init__(self, application_id: str) -> None:
        self.application_id = application_id
        self.version: int = 0
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

    def _on_ComplianceCheckRequested(self, event: StoredEvent) -> None:
        self.regulation_set_version = event.payload.get("regulation_set_version")
        self.checks_required = set(event.payload.get("checks_required", []))

    def _on_ComplianceRulePassed(self, event: StoredEvent) -> None:
        self.passed_rules.add(event.payload.get("rule_id", ""))

    def _on_ComplianceRuleFailed(self, event: StoredEvent) -> None:
        self.failed_rules.add(event.payload.get("rule_id", ""))

    def all_required_checks_passed(self) -> bool:
        """True if every required check has a passed rule (and no required check failed)."""
        if not self.checks_required:
            return False
        return self.checks_required.issubset(self.passed_rules) and not (self.checks_required & self.failed_rules)

    def assert_can_clear(self) -> None:
        """Cannot issue clearance without all mandatory checks passed."""
        if not self.all_required_checks_passed():
            raise DomainError(
                "All mandatory compliance checks must pass before clearance",
                code="COMPLIANCE_INCOMPLETE",
            )
