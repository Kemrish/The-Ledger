"""
MCP tool handlers (command side of CQRS).

Each async function is suitable for binding to an MCP server (e.g. FastMCP). Return shape:
  - Success: `{ "ok": true, ... }` or stream-specific ids
  - Failure: `{ "error": { "error_type", "message", ... } }` — `DomainError` / `OptimisticConcurrencyError`
    include structured fields for LLM recovery (`suggested_action`, `code`, `stream_id`, …).

Preconditions (representative):
  - `start_agent_session` MUST run before `record_credit_analysis` / `record_fraud_screening` / `generate_decision`
    (Gas Town: AgentContextLoaded first).
  - `submit_application` before domain commands that reference `application_id`.
  - `generate_decision` requires prior credit + fraud analyses and compliance cleared per aggregate rules.
"""
from __future__ import annotations

from ..commands.handlers import (
    handle_submit_application,
    handle_credit_analysis_completed,
    handle_fraud_screening_completed,
    handle_compliance_check,
    handle_generate_decision,
    handle_human_review_completed,
    handle_start_agent_session,
)
from ..commands.models import (
    SubmitApplicationCommand,
    CreditAnalysisCompletedCommand,
    FraudScreeningCompletedCommand,
    ComplianceCheckCommand,
    GenerateDecisionCommand,
    HumanReviewCompletedCommand,
    StartAgentSessionCommand,
)
from ..integrity.audit_chain import run_integrity_check
from ..models.events import DomainError, OptimisticConcurrencyError
from ..agents.gemini_decision_agent import GeminiDecisionAgent


def _tool_error(exc: Exception) -> dict:
    if isinstance(exc, (DomainError, OptimisticConcurrencyError)):
        return exc.to_dict()
    return {
        "error_type": type(exc).__name__,
        "message": str(exc),
    }


async def submit_application(store, payload: dict):
    try:
        cmd = SubmitApplicationCommand(**payload)
        stream_id, version = await handle_submit_application(cmd, store)
        return {"ok": True, "stream_id": stream_id, "initial_version": version}
    except Exception as exc:
        return {"ok": False, "error": _tool_error(exc)}


async def record_credit_analysis(store, payload: dict):
    try:
        # Gemini assist path: synthesize missing analysis fields.
        if (
            payload.get("risk_tier") is None
            or payload.get("recommended_limit_usd") is None
            or payload.get("confidence_score") is None
        ):
            agent = GeminiDecisionAgent()
            g = agent.analyze_credit(payload)
            payload = {
                **payload,
                "risk_tier": payload.get("risk_tier") or g.risk_tier,
                "recommended_limit_usd": payload.get("recommended_limit_usd", g.recommended_limit_usd),
                "confidence_score": payload.get("confidence_score", g.confidence_score),
                "duration_ms": payload.get("duration_ms", g.analysis_duration_ms),
            }
        cmd = CreditAnalysisCompletedCommand(**payload)
        await handle_credit_analysis_completed(cmd, store)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": _tool_error(exc)}


async def record_fraud_screening(store, payload: dict):
    try:
        # Gemini assist path: infer fraud score/flags when omitted.
        if payload.get("fraud_score") is None:
            agent = GeminiDecisionAgent()
            g = agent.analyze_fraud(payload)
            payload = {
                **payload,
                "fraud_score": g.fraud_score,
                "anomaly_flags": payload.get("anomaly_flags") or g.anomaly_flags,
            }
        cmd = FraudScreeningCompletedCommand(**payload)
        await handle_fraud_screening_completed(cmd, store)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": _tool_error(exc)}


async def record_compliance_check(store, payload: dict):
    try:
        # Gemini assist path: produce concise failure reason when failed rule is supplied
        # without an explicit reason.
        if payload.get("failed_rule_id") and not payload.get("failure_reason"):
            agent = GeminiDecisionAgent()
            g = agent.summarize_compliance_failure(payload)
            payload = {
                **payload,
                "failure_reason": g.failure_reason,
                "remediation_required": payload.get("remediation_required", g.remediation_required),
            }
        cmd = ComplianceCheckCommand(**payload)
        await handle_compliance_check(cmd, store)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": _tool_error(exc)}


async def generate_decision(store, payload: dict):
    try:
        # If caller omits recommendation/summary, use Gemini to synthesize them.
        if not payload.get("recommendation") or not payload.get("decision_basis_summary"):
            agent = GeminiDecisionAgent()
            decision = agent.generate_decision(payload)
            payload = {
                **payload,
                "recommendation": payload.get("recommendation") or decision.recommendation,
                "decision_basis_summary": payload.get("decision_basis_summary") or decision.decision_basis_summary,
                "confidence_score": payload.get("confidence_score", decision.confidence_score),
            }
        cmd = GenerateDecisionCommand(**payload)
        await handle_generate_decision(cmd, store)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": _tool_error(exc)}


async def record_human_review(store, payload: dict):
    try:
        cmd = HumanReviewCompletedCommand(**payload)
        await handle_human_review_completed(cmd, store)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": _tool_error(exc)}


async def start_agent_session(store, payload: dict):
    try:
        cmd = StartAgentSessionCommand(**payload)
        stream_id, context_position = await handle_start_agent_session(cmd, store)
        return {"ok": True, "session_id": stream_id, "context_position": context_position}
    except Exception as exc:
        return {"ok": False, "error": _tool_error(exc)}


async def run_integrity_check_tool(store, payload: dict):
    try:
        result = await run_integrity_check(store, payload["entity_type"], payload["entity_id"])
        return {
            "ok": result.chain_valid and not result.tamper_detected,
            "check_result": {
                "events_verified": result.events_verified,
                "integrity_hash": result.integrity_hash,
            },
            "chain_valid": result.chain_valid,
            "tamper_detected": result.tamper_detected,
        }
    except Exception as exc:
        return {"ok": False, "error": _tool_error(exc)}
