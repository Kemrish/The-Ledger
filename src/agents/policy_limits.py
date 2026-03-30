"""
Deterministic internal **bank policy & limits** evaluation (distinct from regulatory compliance).

Used by the policy agent to emit `PolicyEvaluationCompleted` with auditable rule outcomes.
"""
from __future__ import annotations

from typing import Any

# Aligns with facts-demo fraud gate; stricter uses get a REFER/BLOCK first.
_POLICY_VERSION = "2026-Q1"


def evaluate_bank_policy(
    *,
    loan_purpose: str,
    requested_amount_usd: float,
    risk_tier: str,
    fraud_score: float,
) -> dict[str, Any]:
    """
    Returns recommended_action: PASS | REFER | BLOCK, policy_passed (not BLOCK), violations, details.
    """
    violations: list[str] = []
    tier = (risk_tier or "MEDIUM").upper()
    purpose = (loan_purpose or "").lower()
    amt = float(requested_amount_usd or 0.0)
    fs = float(fraud_score or 0.0)

    details: dict[str, Any] = {
        "policy_set_version": _POLICY_VERSION,
        "inputs": {
            "loan_purpose": loan_purpose,
            "requested_amount_usd": amt,
            "risk_tier": tier,
            "fraud_score": fs,
        },
    }

    # POL-FRAUD: same band as automated decline gate in facts demos
    if fs >= 0.40:
        violations.append("POL-FRAUD: fraud_score >= 0.40 → BLOCK")

    # POL-PURPOSE: prohibited categories
    if any(x in purpose for x in ("crypto", "cryptocurrency", "illicit")):
        violations.append("POL-PURPOSE: prohibited loan purpose category")

    # POL-SIZE / risk: very large HIGH-risk requests need manual review
    if tier == "HIGH" and amt >= 1_500_000.0:
        violations.append("POL-SIZE: HIGH risk + requested amount >= $1.5M → REFER")

    # Escalate BLOCK conditions
    if any("BLOCK" in v for v in violations):
        return {
            "policy_set_version": _POLICY_VERSION,
            "policy_passed": False,
            "recommended_action": "BLOCK",
            "policy_violations": violations,
            "details": details,
        }

    # REFER-only (no hard block)
    if any("REFER" in v for v in violations):
        return {
            "policy_set_version": _POLICY_VERSION,
            "policy_passed": True,
            "recommended_action": "REFER",
            "policy_violations": violations,
            "details": details,
        }

    return {
        "policy_set_version": _POLICY_VERSION,
        "policy_passed": True,
        "recommended_action": "PASS",
        "policy_violations": [],
        "details": details,
    }
