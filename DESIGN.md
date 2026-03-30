# The Ledger — Design

This document satisfies assessment criteria for **schema justification**, **CQRS/projection strategy**, **concurrency**, **upcasting**, **integrity**, and **MCP** interfaces. It complements `DOMAIN_NOTES.md` (conceptual) with **implementation-specific** decisions.

---

## 1. Event store schema & database foundation

### Required tables

| Table | Role |
|-------|------|
| `events` | Append-only fact log; one row per domain event. |
| `event_streams` | Per-stream metadata and **optimistic concurrency** cursor (`current_version`). |
| `projection_checkpoints` | Per-projection resume position (`last_position` = last processed `global_position`). |
| `outbox` | Transactional outbox; one row per committed event for reliable downstream publish. |

### Column justification (summary)

- **`events`**: `event_id` (UUID) stable id for references/outbox; `stream_id` + `stream_position` (unique) define ordering within aggregate; `global_position` (identity) gives total order for projectors; `event_type` / `event_version` support deserialization and **upcasting**; `payload` / `metadata` JSONB hold domain data and correlation/causation; `recorded_at` is server commit time for audit timelines (not client-supplied).
- **`event_streams`**: `aggregate_type` supports routing/monitoring; `current_version` is the **expected_version** cursor; `archived_at` allows policy to block new appends without deleting history; `metadata` for tenant/tags.
- **`projection_checkpoints`**: minimal resume state for async projectors.
- **`outbox`**: `destination` routes to bus/connectors; `published_at` / `attempts` support retry workers.

### Constraints & indexes

- **Primary keys / uniqueness**: `events.uq_stream_position` prevents duplicate positions; `events.event_id` primary key.
- **Foreign keys**: `events.stream_id → event_streams.stream_id` (restrict delete); `outbox.event_id → events.event_id`.
- **Indexes** (per spec): `idx_events_stream_id (stream_id, stream_position)`, `idx_events_global_pos (global_position)`, `idx_events_type (event_type)`, `idx_events_recorded (recorded_at)`; outbox partial index for pending work.

### Append-only & auditability

- No `UPDATE`/`DELETE` on `events` in application code; only `INSERT`.
- `recorded_at` defaults to `clock_timestamp()` for tamper-evident timelines (combined with integrity chain in `src/integrity/audit_chain.py`).

### Future schema improvements

- **Hot/cold split**: partition `events` by `recorded_at` or `global_position` range for very large logs.
- **Outbox idempotency**: unique constraint on `(destination, event_id)` if publishers need deduplication.
- **Stream metadata versioning**: explicit schema for `event_streams.metadata` if multi-tenant routing grows.

---

## 2. `EventStore` — append & optimistic concurrency

- **`append()`** (see `src/event_store.py`): runs in a **single `asyncpg` transaction**; `SELECT ... FOR UPDATE` on `event_streams`; validates `expected_version` (`-1` only when stream is new at version 0); rejects with `OptimisticConcurrencyError` on mismatch; inserts events; updates `current_version`; writes **one outbox row per event** in the **same transaction**.
- **Tests**: `tests/test_concurrency.py` — double append; exactly one succeeds; loser gets `OptimisticConcurrencyError`.

---

## 3. `EventStore` — load & replay

- **`load_stream`**: ordered by `stream_position ASC`; optional `to_position` for partial replay.
- **`load_all`**: batched async generator over `global_position` for projector catch-up.
- **`stream_version`**: reads `event_streams.current_version`.
- **Upcasting**: if `UpcasterRegistry` is set, `load_stream` / `load_all` return **upcast** `StoredEvent` instances; **database payload bytes are unchanged** (`tests/phase4/test_upcasting.py`).

---

## 4. Aggregates & business rules

- Four aggregates: **`LoanApplicationAggregate`**, **`AgentSessionAggregate`**, **`ComplianceRecordAggregate`**, **`AuditLedgerAggregate`** — each with `load()` → replay via `_apply()`, explicit **state enums** (`ApplicationState`, `SessionState`, `ComplianceState`, `AuditLedgerState`), and `DomainError` on invalid transitions or replay.
- **LoanApplication — six business rules (BR1–BR6)** implemented as `br*_enforce_*` / `br*_resolve_*` on the aggregate only: **BR1** unique stream; **BR2** credit analysis eligibility; **BR3** complete analyses before decision; **BR4** compliance cleared before funding; **BR5** approved amount ≤ agent limit; **BR6** causal contributing sessions + confidence floor. Handlers call these methods; they do not duplicate rules.
- **Compliance**: `build_events_for_command()` returns events to append from command + aggregate state.
- **Gas Town**: `AgentSession` — `CreditAnalysisCompleted` / `FraudScreeningCompleted` before `AgentContextLoaded` fails replay with `GAS_TOWN_VIOLATION`.

---

## 5. Projection daemon & CQRS read models

- **`ProjectionDaemon`** (`src/projections/daemon.py`): background `asyncio` loop; advisory locks per projection; checkpoints; **lag** via `get_lag` / `get_all_lags`; retries then skips poison events.
- **Projections**: `ApplicationSummary`, `AgentPerformanceLedger`, `ComplianceAuditView` — tables in `src/schema.sql`.
- **Compliance temporal query**: `ComplianceAuditProjection.get_compliance_at(store, application_id, as_of)` uses `as_of_recorded_at` and `as_of_event_position` history. The Ledger UI exposes this via `GET /api/compliance/as_of` (and `GET /api/compliance/history` for recent snapshots); see `src/ui/server.py`.
- **Rebuild**: each projection exposes `rebuild_from_scratch`; `rebuild_projections_full()` truncates and resets checkpoints; `rebuild_projections_from_scratch()` additionally runs `ProjectionDaemon` until lag is zero.
- **Load SLO**: `tests/phase3/test_projection_load_slo.py` — concurrent submits (connection pool), bounded catch-up time, then `rebuild_projections_from_scratch` with row-count assertions.

---

## 6. Upcaster registry & cryptographic integrity

- **`UpcasterRegistry`** (`src/upcasting/registry.py`): chain of `(event_type, version)` → payload transforms.
- **Upcasters** (`src/upcasting/upcasters.py`): `CreditAnalysisCompleted` v1→v2, `DecisionGenerated` v1→v2 — **unknown historical fields use JSON `null`**, not synthetic placeholder strings.
- **Immutability**: `tests/phase4/test_upcasting.py` — append-path JSON fingerprint unchanged; raw SQL v1 row `payload::text` and `event_version` unchanged after load while in-memory upcast sees v2.
- **Audit chain** (`src/integrity/audit_chain.py`): `verify_audit_chain` recomputes every stored checkpoint; `run_integrity_check` verifies first, then appends a new `AuditIntegrityCheckRun` only if the chain is valid (otherwise returns `chain_valid=False`, `tamper_detected=True` with **no** append). Hashes chain previous `integrity_hash` with ordered primary-stream event digests on `audit-{entity_type}-{entity_id}`. **Convention**: for loan applications use `entity_type="loan"` and `entity_id=<application_id>` so the primary stream is `loan-{id}`.

---

## 7. Gas Town — agent memory reconstruction

- **`reconstruct_agent_context`** (`src/integrity/gas_town.py`): loads full `agent-{agent_id}-{session_id}` stream; **compact lines** for older events; **verbatim JSON** for the last `verbatim_recent` events (default 3); flags **`NEEDSRECONCILIATION`** (`SESSION_NEEDS_RECONCILIATION`) when the last event looks failed/requested/incomplete or `DecisionGenerated` with `REFER`.

---

## 8. MCP — tools, resources, LLM-consumability

- **Tools** (`src/mcp/tools.py`): success returns `{"ok": true, ...}`; failures `{"ok": false, "error": {...}}`. `DomainError` / `OptimisticConcurrencyError` serialize via `to_dict()` (`error_type`, `code`, `stream_id`, `suggested_action`, …). `run_integrity_check_tool` adds `tamper_detected`.
- **Specs** (`src/mcp/specs.py`): `TOOL_SPECS` / `RESOURCE_SPECS` — JSON-schema-shaped inputs, **preconditions**, and success shapes for LLM-oriented registration.
- **Resources** (`src/mcp/resources.py`): read **projections** (and justified direct stream load for audit trail / agent session replay).
- **Lifecycle test** (`tests/phase5/test_mcp_lifecycle.py`): drives submit → sessions → credit → fraud → decision → human review → integrity → application resource **via the same Python functions the MCP server binds to tools** (transport-agnostic contract).

---

## 9. Operational notes

- Apply schema: `psql` / `asyncpg` executing `src/schema.sql`.
- Environment: `LEDGER_TEST_DSN` or `DATABASE_URL` for CI/local Postgres.
