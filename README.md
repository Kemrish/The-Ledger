# The Ledger — Agentic Event Store & Enterprise Audit Infrastructure

Event sourcing and audit backbone for multi-agent AI systems (Apex Financial Services scenario).

**Design & rubric mapping:** see [`DESIGN.md`](DESIGN.md) (schema justification, CQRS, concurrency, upcasting, MCP). Conceptual notes remain in `DOMAIN_NOTES.md` if present.

## Setup

- **Python 3.11+**
- **PostgreSQL** (for event store and tests)
- **Gemini API key** (optional unless using Gemini-backed decision generation)

### Install (uv)

```bash
cd "The Ledger"
uv sync
```

### Database

Create a test database and set the DSN (optional; default below):

```bash
# Example: create database
createdb ledger_test

# Optional: override DSN
set LEDGER_TEST_DSN=postgresql://user:pass@localhost:5432/ledger_test
```

### Environment variables

Create `.env` from `.env.example` and fill your keys/settings:

```bash
copy .env.example .env
```

Required when using Gemini-backed tool flow:

- `GEMINI_API_KEY`
- `.env` is auto-loaded via `python-dotenv` in `GeminiDecisionAgent`; no hardcoded keys in source.

`generate_decision` in `src/mcp/tools.py` now supports Gemini synthesis:
- If `recommendation` or `decision_basis_summary` is omitted in payload, it calls Gemini.
- If both are provided, it uses caller-provided values (no external LLM call).

Gemini is also used as assistive inference in additional MCP tool paths:
- `record_credit_analysis`: can infer `risk_tier`, `recommended_limit_usd`, `confidence_score` when missing.
- `record_fraud_screening`: can infer `fraud_score` / `anomaly_flags` when missing.
- `record_compliance_check`: can draft `failure_reason` for failed rules when omitted.

Deterministic domain rules in command handlers/aggregates still remain authoritative.

### Migrations

Apply the schema once:

```bash
psql -d ledger_test -f src/schema.sql
```

Or from Python (e.g. in tests or a script):

```python
import asyncio, asyncpg
with open("src/schema.sql") as f:
    schema = f.read()
async def run():
    conn = await asyncpg.connect("postgresql://postgres:postgres@localhost:5432/ledger_test")
    await conn.execute(schema)
    await conn.close()
asyncio.run(run())
```

### Tests

From project root (with `PYTHONPATH` so `src` is importable):

```bash
uv run pytest tests/ -v
```

Or:

```bash
set PYTHONPATH=%CD%
uv run pytest tests/ -v
```

Required: PostgreSQL running and schema applied (tests apply schema in fixtures).

### CI (GitHub Actions)

On push/PR to `main` or `master`, the workflow runs tests against a Postgres 16 service and uploads the test log as an artifact. CI uses `pytest -s` so the concurrency test’s **`CONCURRENCY_TEST_PROOF`** stdout block appears in `test-output.log`.

1. Push the repo to GitHub and open the **Actions** tab.
2. After the run finishes, open the latest workflow run → **Summary**.
3. Under **Artifacts**, download **test-log** (contains `test-output.log` and `test-results.xml`).
4. Use `test-output.log` (or a screenshot of it) in your interim PDF as “Concurrency test results”.

You can also trigger a run manually: **Actions** → **CI** → **Run workflow**.

## Phase 2 — Domain Logic

- **Aggregates:** `LoanApplication`, `AgentSession`, `ComplianceRecord`, `AuditLedger` (load from store, apply events, enforce invariants).
- **Command handlers:** Submit application, start agent session, credit analysis, fraud screening, compliance check, generate decision, human review (load → validate → append).
- **Business rules:** State machine transitions, Gas Town (context loaded before decisions), confidence floor (REFER if &lt; 0.6), compliance cleared before approval, causal chain for contributing sessions.

## Phase 3–5 Baseline

- **Projections:** `src/projections/daemon.py`, `src/projections/application_summary.py`, `src/projections/agent_performance.py`, `src/projections/compliance_audit.py`
- **Upcasting:** `src/upcasting/registry.py`, `src/upcasting/upcasters.py` (read-time upcasts)
- **Integrity:** `src/integrity/audit_chain.py` (hash-chain event), `src/integrity/gas_town.py` (session context reconstruction)
- **MCP interface modules:** `src/mcp/tools.py`, `src/mcp/resources.py`, `src/mcp/server.py` (runtime entry)

## Structure

- `src/schema.sql` — Event store tables (events, event_streams, projection_checkpoints, outbox).
- `src/event_store.py` — `EventStore` (append with expected_version, load_stream, load_all, stream_version, archive, get_stream_metadata).
- `src/models/events.py` — Event types, `StoredEvent`, `StreamMetadata`, `OptimisticConcurrencyError`, `DomainError`.
- `src/aggregates/` — Loan application, agent session, compliance record, audit ledger.
- `src/commands/` — Command models and handlers.
- `tests/test_concurrency.py` — Double-decision optimistic concurrency test.
- `tests/test_phase2_handlers.py` — Phase 2 handler and aggregate flow.
- `tests/phase3/test_projections_daemon.py` — Projection daemon baseline.
- `tests/phase4/test_upcasting.py` — Upcasting immutability baseline.
- `tests/phase4/test_gas_town.py` — Crash recovery context reconstruction baseline.
- `tests/phase5/test_mcp_lifecycle.py` — MCP tool-driven lifecycle baseline.

## UI (Demo)

This project includes a minimal web UI that runs the projection daemon and lets you append a full loan lifecycle via the backend.

### Run

1. Ensure Postgres is running and the schema is applied:

```bash
psql -d ledger_test -f src/schema.sql
```

2. Start the UI server:

```bash
uv run uvicorn src.ui.server:app --reload --port 8766
```

3. Open `http://localhost:8766` in your browser.
4. Click **“Run demo”** to execute a deterministic end-to-end lifecycle and display the projection row.
