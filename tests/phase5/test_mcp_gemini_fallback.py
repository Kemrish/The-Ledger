import pytest

from src.mcp import tools


class _DummyGemini:
    def analyze_credit(self, payload):
        class O:
            risk_tier = "MEDIUM"
            recommended_limit_usd = 12345.0
            confidence_score = 0.77
            analysis_duration_ms = 222

        return O()

    def analyze_fraud(self, payload):
        class O:
            fraud_score = 0.22
            anomaly_flags = ["UNUSUAL_MARGIN_DELTA"]

        return O()

    def summarize_compliance_failure(self, payload):
        class O:
            remediation_required = True
            failure_reason = "Required compliance document missing."

        return O()

    def generate_decision(self, payload):
        class O:
            recommendation = "REFER"
            decision_basis_summary = "Mixed signals from credit and fraud checks."
            confidence_score = 0.58

        return O()


@pytest.mark.asyncio
async def test_record_credit_analysis_uses_gemini_fallback(monkeypatch):
    captured = {}

    async def _fake_handle(cmd, store):
        captured["cmd"] = cmd

    monkeypatch.setattr(tools, "GeminiDecisionAgent", lambda: _DummyGemini())
    monkeypatch.setattr(tools, "handle_credit_analysis_completed", _fake_handle)

    payload = {
        "application_id": "app-1",
        "agent_id": "credit",
        "session_id": "s1",
        "model_version": "v2",
        # Missing: risk_tier, recommended_limit_usd, confidence_score
    }
    res = await tools.record_credit_analysis(store=None, payload=payload)
    assert res["ok"] is True
    assert captured["cmd"].risk_tier == "MEDIUM"
    assert captured["cmd"].recommended_limit_usd == 12345.0
    assert captured["cmd"].confidence_score == 0.77
    assert captured["cmd"].duration_ms == 222


@pytest.mark.asyncio
async def test_record_fraud_screening_uses_gemini_fallback(monkeypatch):
    captured = {}

    async def _fake_handle(cmd, store):
        captured["cmd"] = cmd

    monkeypatch.setattr(tools, "GeminiDecisionAgent", lambda: _DummyGemini())
    monkeypatch.setattr(tools, "handle_fraud_screening_completed", _fake_handle)

    payload = {
        "application_id": "app-1",
        "agent_id": "fraud",
        "session_id": "s1",
        "screening_model_version": "v1",
        # Missing fraud_score
    }
    res = await tools.record_fraud_screening(store=None, payload=payload)
    assert res["ok"] is True
    assert captured["cmd"].fraud_score == 0.22
    assert captured["cmd"].anomaly_flags == ["UNUSUAL_MARGIN_DELTA"]


@pytest.mark.asyncio
async def test_record_compliance_check_uses_gemini_failure_reason(monkeypatch):
    captured = {}

    async def _fake_handle(cmd, store):
        captured["cmd"] = cmd

    monkeypatch.setattr(tools, "GeminiDecisionAgent", lambda: _DummyGemini())
    monkeypatch.setattr(tools, "handle_compliance_check", _fake_handle)

    payload = {
        "application_id": "app-1",
        "regulation_set_version": "2026-Q1",
        "checks_required": ["REG-001"],
        "failed_rule_id": "REG-001",
        "failed_rule_version": "1",
        # Missing failure_reason
    }
    res = await tools.record_compliance_check(store=None, payload=payload)
    assert res["ok"] is True
    assert captured["cmd"].failure_reason == "Required compliance document missing."
    assert captured["cmd"].remediation_required is True


@pytest.mark.asyncio
async def test_generate_decision_uses_gemini_when_fields_missing(monkeypatch):
    captured = {}

    async def _fake_handle(cmd, store):
        captured["cmd"] = cmd

    monkeypatch.setattr(tools, "GeminiDecisionAgent", lambda: _DummyGemini())
    monkeypatch.setattr(tools, "handle_generate_decision", _fake_handle)

    payload = {
        "application_id": "app-1",
        "orchestrator_agent_id": "orch-1",
        "session_id": "s1",
        "contributing_agent_sessions": ["agent-credit-s1"],
        "model_versions": {"credit": "v2"},
        # Missing recommendation + decision_basis_summary
    }
    res = await tools.generate_decision(store=None, payload=payload)
    assert res["ok"] is True
    assert captured["cmd"].recommendation == "REFER"
    assert captured["cmd"].decision_basis_summary.startswith("Mixed signals")
    assert captured["cmd"].confidence_score == 0.58
