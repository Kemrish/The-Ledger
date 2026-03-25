from __future__ import annotations

from ..event_store import EventStore
from .registry import UpcasterRegistry


registry = UpcasterRegistry()


@registry.register("CreditAnalysisCompleted", from_version=1)
def upcast_credit_v1_to_v2(payload: dict) -> dict:
    """
    Infer v2 fields without inventing domain strings: use JSON null for unknown historical values.
    """
    out = dict(payload)
    if "model_version" not in out or out.get("model_version") in (None, ""):
        out["model_version"] = None
    if "regulatory_basis" not in out:
        out["regulatory_basis"] = None
    elif out.get("regulatory_basis") in (None, ""):
        out["regulatory_basis"] = None
    return out


@registry.register("DecisionGenerated", from_version=1)
def upcast_decision_v1_to_v2(payload: dict) -> dict:
    out = dict(payload)
    if "model_versions" not in out or out.get("model_versions") is None:
        out["model_versions"] = None
    return out


def create_store_with_upcasters(conn) -> EventStore:
    return EventStore(conn, upcaster_registry=registry)
