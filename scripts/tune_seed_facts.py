import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any


def _parse_dt(s: str | None) -> datetime:
    if not s:
        return datetime.fromtimestamp(0)
    # seed_events uses ISO timestamps with microseconds
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime.fromtimestamp(0)


def _read_seed_events(path: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _seed_final_decision(seed_events: list[dict[str, Any]]) -> dict[str, str]:
    """
    Ground truth based on seed’s terminal events.
    For each loan-{seed_app_id} stream:
      - if last outcome is ApplicationApproved => APPROVE
      - if last outcome is ApplicationDeclined => DECLINE
    """
    outcomes: dict[str, tuple[datetime, str]] = {}
    for ev in seed_events:
        stream_id = ev.get("stream_id") or ""
        if not stream_id.startswith("loan-"):
            continue
        app_id = stream_id[len("loan-") :]
        et = ev.get("event_type")
        if et not in ("ApplicationApproved", "ApplicationDeclined"):
            continue
        ts = _parse_dt(ev.get("recorded_at"))
        kind = "APPROVE" if et == "ApplicationApproved" else "DECLINE"
        prev = outcomes.get(app_id)
        if prev is None or ts >= prev[0]:
            outcomes[app_id] = (ts, kind)
    return {k: v for k, (_, v) in outcomes.items()}


@dataclass
class GateParams:
    fraud_decline_score: float = 0.40
    low_confidence_decline: float = 0.60
    credit_limit_ratio_decline: float | None = 0.90


def _risk_order(tier: str) -> int:
    return {"LOW": 0, "MEDIUM": 1, "HIGH": 2}.get(str(tier).upper(), 1)


def predict_from_facts(ui_server: Any, seed_app_id: str, gate: GateParams) -> dict[str, Any]:
    seed_data = ui_server._seed_extract_minimal_for_facts(seed_app_id)
    facts = ui_server._seed_extract_facts(seed_app_id)

    requested_amount = float(seed_data["submitted"]["requested_amount_usd"])
    credit_model_version = str(seed_data["credit"]["model_version"] or "v1")
    fraud_model_version = str(seed_data["fraud"]["screening_model_version"] or "v1")

    credit_infer = ui_server._infer_credit_from_facts(
        facts, requested_amount, credit_model_version
    )
    fraud_infer = ui_server._infer_fraud_from_facts(facts, fraud_model_version)
    compliance_infer = ui_server._infer_compliance_from_facts(facts)

    compliance_cleared = bool(compliance_infer["compliance_cleared"])
    risk_tier = str(credit_infer.get("risk_tier") or "MEDIUM")
    fraud_score = float(fraud_infer.get("fraud_score") or 0.0)
    confidence = float(credit_infer.get("confidence_score") or 0.0)
    recommended_limit_usd = float(credit_infer.get("recommended_limit_usd") or 0.0)

    fraud_declines = fraud_score >= gate.fraud_decline_score
    low_confidence_declines = confidence < gate.low_confidence_decline

    credit_limit_declines = False
    if gate.credit_limit_ratio_decline is not None and requested_amount > 0:
        credit_limit_declines = (recommended_limit_usd / requested_amount) < gate.credit_limit_ratio_decline

    should_approve = compliance_cleared and not (
        fraud_declines or low_confidence_declines or credit_limit_declines
    )
    return {
        "risk_tier": risk_tier,
        "fraud_score": fraud_score,
        "confidence": confidence,
        "compliance_cleared": compliance_cleared,
        "compliance_infer": compliance_infer,
        "recommended_limit_ratio": (recommended_limit_usd / requested_amount) if requested_amount > 0 else None,
        "predicted": "APPROVE" if should_approve else "DECLINE",
        "should_approve": should_approve,
    }


def main() -> None:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    seed_path = os.path.join(repo_root, "data", "seed_events.jsonl")

    # Late import so repo root resolution works consistently.
    from src.ui import server as ui_server  # noqa: E402

    seed_events = _read_seed_events(seed_path)
    truth = _seed_final_decision(seed_events)
    seed_ids = sorted(truth.keys())
    print(f"Seed apps with terminal outcome: {len(seed_ids)}")

    gate = GateParams(
        fraud_decline_score=0.40,
        low_confidence_decline=0.60,
        credit_limit_ratio_decline=0.90,
    )

    mismatches: list[tuple[str, str, str, dict[str, Any]]] = []
    for app_id in seed_ids:
        pred = predict_from_facts(ui_server, app_id, gate)
        y_true = truth[app_id]
        y_pred = pred["predicted"]
        if y_true != y_pred:
            mismatches.append((app_id, y_true, y_pred, pred))

    print(f"Mismatches (with credit_limit_ratio gate): {len(mismatches)}")
    for app_id, y_true, y_pred, pred in mismatches[:20]:
        c = pred.get("compliance_infer") or {}
        print(
            f"- {app_id}: truth={y_true} pred={y_pred} "
            f"(risk={pred['risk_tier']} fraud={pred['fraud_score']:.2f} conf={pred['confidence']:.2f} "
            f"compliance={pred['compliance_cleared']} ratio={pred['recommended_limit_ratio']}; "
            f"features: gaap={c.get('gaap_ok')} revenue_ok={c.get('revenue_ok')} liquidity_ok={c.get('liquidity_ok')} "
            f"profitability_ok={c.get('profitability_ok')} solvency_ok={c.get('solvency_ok')} "
            f"d_e={c.get('debt_to_equity')} d_e2b={c.get('debt_to_ebitda')} current_ratio={c.get('current_ratio')} net_margin={c.get('net_margin')})"
        )


if __name__ == "__main__":
    main()

