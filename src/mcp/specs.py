"""
Machine- and LLM-oriented metadata for MCP tools and resources: JSON Schema fragments,
preconditions, and structured error shapes returned by `src.mcp.tools`.
"""

from __future__ import annotations

# Common error envelope from tools: {"error": {"error_type", "message", ...}}
STRUCTURED_ERROR_TYPES = (
    "DomainError",
    "OptimisticConcurrencyError",
    "ValidationError",
)

TOOL_SPECS: list[dict] = [
    {
        "name": "submit_application",
        "description": "Create a new loan application stream (loan-{application_id}).",
        "preconditions": ["application_id must be unique (no existing loan stream)."],
        "input_schema": {
            "type": "object",
            "required": ["application_id", "applicant_id", "requested_amount_usd", "loan_purpose"],
            "properties": {
                "application_id": {"type": "string"},
                "applicant_id": {"type": "string"},
                "requested_amount_usd": {"type": "number"},
                "loan_purpose": {"type": "string"},
                "submission_channel": {"type": "string", "default": "api"},
            },
        },
        "success_shape": {"ok": True, "stream_id": "string", "initial_version": "integer"},
    },
    {
        "name": "start_agent_session",
        "description": "Open an agent session stream; first event must be AgentContextLoaded (Gas Town).",
        "preconditions": ["Session stream agent-{agent_id}-{session_id} must not already exist."],
        "input_schema": {
            "type": "object",
            "required": ["agent_id", "session_id", "context_source", "model_version"],
            "properties": {
                "agent_id": {"type": "string"},
                "session_id": {"type": "string"},
                "context_source": {"type": "string", "enum": ["fresh", "replay"]},
                "event_replay_from_position": {"type": "integer"},
                "context_token_count": {"type": "integer"},
                "model_version": {"type": "string"},
            },
        },
        "success_shape": {"ok": True, "session_id": "string", "context_position": "integer"},
    },
    {
        "name": "record_credit_analysis",
        "description": "Append CreditAnalysisCompleted to agent and loan streams.",
        "preconditions": [
            "Agent session must exist with AgentContextLoaded.",
            "Loan application must be awaiting credit analysis.",
            "model_version must match session context when set.",
        ],
        "input_schema": {
            "type": "object",
            "required": ["application_id", "agent_id", "session_id", "model_version"],
            "properties": {
                "application_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "session_id": {"type": "string"},
                "model_version": {"type": "string"},
                "confidence_score": {"type": ["number", "null"]},
                "risk_tier": {"type": ["string", "null"]},
                "recommended_limit_usd": {"type": ["number", "null"]},
                "duration_ms": {"type": "integer"},
                "input_data": {"type": "object"},
            },
        },
        "success_shape": {"ok": True},
    },
    {
        "name": "record_fraud_screening",
        "description": "Append FraudScreeningCompleted to agent and loan streams.",
        "preconditions": ["Agent session must exist with AgentContextLoaded."],
        "input_schema": {
            "type": "object",
            "required": ["application_id", "agent_id", "session_id"],
            "properties": {
                "application_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "session_id": {"type": "string"},
                "fraud_score": {"type": ["number", "null"]},
                "anomaly_flags": {"type": "array", "items": {"type": "string"}},
                "screening_model_version": {"type": "string"},
                "input_data_hash": {"type": "string"},
            },
        },
        "success_shape": {"ok": True},
    },
    {
        "name": "record_policy_evaluation",
        "description": "Append PolicyEvaluationCompleted (internal bank policy) to agent and loan streams.",
        "preconditions": [
            "Agent session must exist with AgentContextLoaded.",
            "Credit and fraud analyses must be complete on the loan aggregate (BR3).",
        ],
        "input_schema": {
            "type": "object",
            "required": [
                "application_id",
                "agent_id",
                "session_id",
                "model_version",
                "loan_purpose",
                "requested_amount_usd",
                "risk_tier",
                "fraud_score",
            ],
            "properties": {
                "application_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "session_id": {"type": "string"},
                "model_version": {"type": "string"},
                "loan_purpose": {"type": "string"},
                "requested_amount_usd": {"type": "number"},
                "risk_tier": {"type": "string"},
                "fraud_score": {"type": "number"},
                "duration_ms": {"type": "integer"},
                "input_data": {"type": "object"},
            },
        },
        "success_shape": {"ok": True},
    },
    {
        "name": "record_compliance_check",
        "description": "Append compliance events on compliance-{application_id}.",
        "preconditions": ["Valid compliance command per handlers (rules, evidence)."],
        "input_schema": {"type": "object", "properties": {"application_id": {"type": "string"}}},
        "success_shape": {"ok": True},
    },
    {
        "name": "generate_decision",
        "description": "Append DecisionGenerated after analyses complete and causal chain validated.",
        "preconditions": [
            "Credit, fraud, and bank policy evaluation complete on loan aggregate (BR3).",
            "Each contributing_agent_sessions entry must have contributed analysis for the application.",
        ],
        "input_schema": {
            "type": "object",
            "required": ["application_id", "orchestrator_agent_id", "contributing_agent_sessions"],
            "properties": {
                "application_id": {"type": "string"},
                "orchestrator_agent_id": {"type": "string"},
                "session_id": {"type": "string"},
                "recommendation": {"type": "string"},
                "confidence_score": {"type": "number"},
                "contributing_agent_sessions": {"type": "array", "items": {"type": "string"}},
                "decision_basis_summary": {"type": "string"},
                "model_versions": {"type": "object"},
            },
        },
        "success_shape": {"ok": True},
    },
    {
        "name": "record_human_review",
        "description": "Append HumanReviewCompleted when loan is in PendingDecision.",
        "preconditions": ["Loan aggregate state must be PendingDecision."],
        "input_schema": {
            "type": "object",
            "required": ["application_id", "reviewer_id", "final_decision"],
            "properties": {
                "application_id": {"type": "string"},
                "reviewer_id": {"type": "string"},
                "override": {"type": "boolean"},
                "final_decision": {"type": "string"},
                "override_reason": {"type": ["string", "null"]},
            },
        },
        "success_shape": {"ok": True},
    },
    {
        "name": "run_integrity_check_tool",
        "description": "Verify audit hash chain for a primary entity stream and append a new checkpoint.",
        "preconditions": ["entity_type and entity_id must identify an existing primary stream prefix (e.g. loan + id → loan-{id})."],
        "input_schema": {
            "type": "object",
            "required": ["entity_type", "entity_id"],
            "properties": {
                "entity_type": {"type": "string"},
                "entity_id": {"type": "string"},
            },
        },
        "success_shape": {
            "chain_valid": "boolean",
            "check_result": {"events_verified": "integer", "integrity_hash": "string"},
        },
    },
]

RESOURCE_SPECS: list[dict] = [
    {
        "name": "application_resource",
        "description": "Latest ApplicationSummary row from projection (not raw stream).",
        "params": {"application_id": "string"},
    },
    {
        "name": "compliance_resource",
        "description": "ComplianceAuditView as-of optional timestamp.",
        "params": {"application_id": "string", "as_of": "ISO-8601 string optional"},
    },
    {
        "name": "audit_trail_resource",
        "description": "Raw audit ledger stream for entity.",
        "params": {"entity_type": "string", "entity_id": "string"},
    },
    {
        "name": "agent_performance_resource",
        "description": "AgentPerformanceLedger rows for an agent.",
        "params": {"agent_id": "string"},
    },
    {
        "name": "session_resource",
        "description": "Raw agent session stream (replay).",
        "params": {"agent_id": "string", "session_id": "string"},
    },
    {
        "name": "ledger_health_resource",
        "description": "Projection lag metrics from daemon.",
        "params": {"daemon": "ProjectionDaemon instance"},
    },
]


def tool_specs_json() -> list[dict]:
    """Return a JSON-serializable list for hosts / LLM tool registration."""
    return TOOL_SPECS
