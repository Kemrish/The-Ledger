# The Ledger — Agentic Event Store & Enterprise Audit Infrastructure

Event sourcing and audit backbone for multi-agent AI systems (Apex Financial Services scenario).

## Setup

- **Python 3.11+**
- **PostgreSQL** (for event store and tests)

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

## Structure

- `src/schema.sql` — Event store tables (events, event_streams, projection_checkpoints, outbox).
- `src/event_store.py` — `EventStore` (append with expected_version, load_stream, load_all, stream_version, archive, get_stream_metadata).
- `src/models/events.py` — Event types, `StoredEvent`, `StreamMetadata`, `OptimisticConcurrencyError`, `DomainError`.
- `src/aggregates/` — Loan application, agent session, compliance record, audit ledger.
- `src/commands/` — Command models and handlers.
- `tests/test_concurrency.py` — Double-decision optimistic concurrency test.
- `tests/test_phase2_handlers.py` — Phase 2 handler and aggregate flow.
