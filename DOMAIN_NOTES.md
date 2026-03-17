# DOMAIN_NOTES.md — The Ledger (Phase 0)

**Apex Financial Services · Event Store & Audit Infrastructure**  
Produced before any implementation code. Answers are specific to this scenario and stack.

---

## 1. EDA vs. ES distinction

**Question:** A component uses callbacks (like LangChain traces) to capture event-like data. Is this Event-Driven Architecture (EDA) or Event Sourcing (ES)? If you redesigned it using The Ledger, what exactly would change in the architecture and what would you gain?

**Answer:**

It is **Event-Driven Architecture (EDA)**. In EDA, events are *messages* between components: one component emits an event (e.g. via a callback or message bus) and another may or may not consume it. The sender does not treat the event as the system’s source of truth; delivery can be best-effort, and events can be dropped, duplicated, or reordered. LangChain-style callbacks that write to a trace store or log are EDA: they produce event-like records, but the *authoritative state* of the system lives elsewhere (e.g. in a CRUD database or in-memory). If the callback fails or the trace store is cleared, the “event” is simply lost.

**What would change with The Ledger (ES):**

- **Persistence contract:** Every material fact (e.g. “CreditAnalysisCompleted for application X”) would be **appended to the event store in the same logical unit of work** as the action that produced it. No fire-and-forget; the write to the store would be part of the success criteria for the operation.
- **Ordering and identity:** Events would be stored in a **single append-only log** (or per-stream logs) with a **global position** and **stream position**, so ordering and “what happened” are defined by the store, not by best-effort messaging.
- **Read path:** To answer “what is the state of application X?” or “what did the agent do?”, the system would **replay the event stream** (or query projections built from it) instead of relying on a separate trace store or callback log that might be incomplete or non-authoritative.
- **Concurrency:** Appends would use **optimistic concurrency** (`expected_version`); conflicting appends would get `OptimisticConcurrencyError` and retry after reload, instead of overwriting or silently diverging.

**What we would gain:**

- **Immutability and auditability:** A single, append-only record of every decision and the data that informed it; no “we lost the trace” failure mode.
- **Reproducibility:** Any prior state can be reconstructed by replaying events up to a point in time.
- **Temporal and what-if queries:** “State at time T” and “what if we had used a different model?” become well-defined by replay over the store.
- **Gas Town prevention:** Agent context can be reconstructed from the event stream after a crash, instead of being lost when the process ends.

---

## 2. The aggregate question

**Question:** In the scenario you will build four aggregates. Identify one alternative boundary you considered and rejected. What coupling problem does your chosen boundary prevent?

**Answer:**

**Chosen boundaries:** LoanApplication, AgentSession, ComplianceRecord, AuditLedger (each with its own stream namespace).

**Alternative considered and rejected:** Merging **ComplianceRecord** into **LoanApplication** — i.e. a single stream per application containing both loan lifecycle events (ApplicationSubmitted, CreditAnalysisCompleted, DecisionGenerated, HumanReviewCompleted, ApplicationApproved/Declined) and compliance events (ComplianceCheckRequested, ComplianceRulePassed, ComplianceRuleFailed).

**Coupling problem this would create:**

- Compliance checks are requested and evaluated in **batches** (e.g. “run all rules in regulation_set_version X”). Multiple ComplianceRulePassed/Failed events can be appended in quick succession for the same application.
- The **DecisionOrchestrator** and **human review** also append to the same application (DecisionGenerated, HumanReviewCompleted, ApplicationApproved).
- With a **single stream**, every append would use **optimistic concurrency** on the same `loan-{application_id}` stream. So:
  - Compliance batch appends 5 rule results → version goes 10 → 15.
  - Meanwhile the orchestrator reads at version 10, computes a decision, and tries to append with `expected_version=10` → **OptimisticConcurrencyError** even though the orchestrator did not conflict with compliance semantically; it only “lost” because compliance wrote first.
- Result: **false contention**. We would see a high rate of concurrency exceptions and retries that are not due to real business conflicts but due to multiple independent writers (compliance engine vs. orchestrator vs. human) sharing one stream.

**What the chosen boundary prevents:**

- **ComplianceRecord** has its own stream `compliance-{application_id}`. Compliance writes only touch that stream; loan lifecycle writes touch `loan-{application_id}`. They do not compete on the same version counter.
- The **LoanApplication** aggregate can still enforce the business rule “cannot approve until compliance is clear” by **reading** the ComplianceRecord stream (or a compliance projection) when validating a command, but the **writes** are on different streams. So we get the invariant without the coupling: no false concurrency conflicts between compliance updates and decision/human-review updates.

---

## 3. Concurrency in practice

**Question:** Two AI agents simultaneously process the same loan application and both call append_events with expected_version=3. Trace the exact sequence of operations in your event store. What does the losing agent receive, and what must it do next?

**Answer:**

**Assumption:** Stream `loan-{application_id}` currently has **3 events** (stream_positions 0, 1, 2). So `current_version = 3` means “the last appended event is at position 2, and the next append will write at position 3” — i.e. we treat version as “number of events in the stream” so that after 3 events, `expected_version=3` is the correct value for the next append. (Alternatively, version = last stream_position, so 3 events → last position 2 → next position 3; then expected_version could be 2 for “I saw up to position 2”. The spec uses expected_version=3, so we take version = stream length: 3 events ⇒ version 3.)

**Exact sequence:**

1. **Agent A** and **Agent B** both load the stream and see 3 events (version 3). Both call `append(..., expected_version=3)` to add one new event.
2. **Agent A**’s append runs first. **Event store:** Within a transaction: (a) SELECT current_version FROM event_streams WHERE stream_id = ? FOR UPDATE → returns 3; (b) CHECK current_version == 3 → true; (c) INSERT one event with stream_position 3; (d) UPDATE event_streams SET current_version = 4; (e) INSERT into outbox; COMMIT.
3. Stream now has **4 events** (positions 0–3).
4. **Agent B**’s append runs. **Event store:** Transaction: (a) SELECT current_version FROM event_streams WHERE stream_id = ? FOR UPDATE → returns **4**; (b) CHECK current_version == 3 → **false**; (c) ROLLBACK; (d) raise **OptimisticConcurrencyError(stream_id=stream_id, expected_version=3, actual_version=4)**.

**What the losing agent receives:**

- An **exception** (or typed error response): `OptimisticConcurrencyError` with at least:
  - `stream_id`
  - `expected_version` (3)
  - `actual_version` (4)
  - A clear `suggested_action` (e.g. `"reload_stream_and_retry"`).

**What the losing agent must do next:**

1. **Catch** the `OptimisticConcurrencyError`.
2. **Reload** the stream: `events = await store.load_stream(stream_id)` (and optionally `version = await store.stream_version(stream_id)`).
3. **Re-apply domain logic** on the updated state: re-run the aggregate’s validation and re-compute whether it still wants to append (e.g. “did the other agent’s CreditAnalysisCompleted already satisfy the need?”).
4. If it still needs to append: call `append(stream_id, events, expected_version=actual_version)` (e.g. 4). If the stream has not moved again, this succeeds; otherwise repeat.
5. Optionally **cap retries** (e.g. 3 attempts) and then return a failure to the caller if the system is under heavy contention.

**Invariant:** The stream ends with **4** events (the original 3 plus the single successful append). Exactly one of the two appends succeeds; the other does not write. So “total events appended to the stream = 4” and we never get 5 (which would happen if both wrote).

---

## 4. Projection lag and its consequences

**Question:** Your LoanApplication projection is eventually consistent with a typical lag of 200ms. A loan officer queries "available credit limit" immediately after an agent commits a disbursement event. They see the old limit. What does your system do, and how do you communicate this to the user interface?

**Answer:**

**What the system does:**

- The **write path** has already succeeded: the disbursement (and any related events) are in the **event store** and the **outbox** in the same transaction. So the authoritative state has changed; the **ApplicationSummary** projection (which derives “available credit limit” or similar) simply has not yet processed that event.
- The **read path** for “available credit limit” is a **query against the ApplicationSummary projection** (CQRS). So the system **returns the current projection row** for that application — which still shows the **old** limit (pre-disbursement). It does *not* block until the projection catches up unless we explicitly offer a “strong consistency” read (see below).
- To support transparency and correctness:
  - The **projection daemon** exposes **lag** per projection (e.g. `get_lag()` → milliseconds between latest event in the store and last event processed by this projection).
  - The **read API** can return **metadata** with the projection result: e.g. `projection_lag_ms`, `last_updated_at` (or `last_event_at` processed for this row), and optionally `consistency_hint: "eventual"`.

**How to communicate this to the UI:**

1. **Return projection data + metadata:**  
   Response body includes the current “available credit limit” (and other fields) **plus** `projection_lag_ms` and `last_updated_at`. The UI can show the value and a small indicator such as “Updated 200ms ago” or “Data may be updating.”
2. **Optional “refreshing” state:**  
   If `projection_lag_ms > threshold` (e.g. 500ms), the UI can show a soft message: “Numbers are catching up…” and either auto-refresh after a short delay or offer a “Refresh” button that re-queries (and eventually sees the new limit once the daemon has processed the disbursement).
3. **Optional strong-consistency read (if we implement it):**  
   For critical screens we could offer a read that waits for the projection to process up to the event that the current user/client just wrote (e.g. wait for `last_global_position` or a per-entity sequence), with a **timeout** (e.g. 2s). Then the UI could call this when the officer just submitted a disbursement and show the new limit without guessing. This is a product/UX choice; the base behaviour is “return projection state + lag so the UI can be honest about staleness.”

**Principle:** We do not hide eventual consistency. We expose lag and last-updated time so the UI can either show the current (possibly stale) value with a clear cue, or trigger a refresh, or use a strong-consistency read where we provide it.

---

## 5. The upcasting scenario

**Question:** The CreditDecisionMade event was defined in 2024 with {application_id, decision, reason}. In 2026 it needs {application_id, decision, reason, model_version, confidence_score, regulatory_basis}. Write the upcaster. What is your inference strategy for historical events that predate model_version?

**Answer:**

*(Note: In our event catalogue the equivalent event is DecisionGenerated; the same pattern applies. Below we use the name from the question, CreditDecisionMade.)*

**Upcaster (conceptual):**

```python
@registry.register("CreditDecisionMade", from_version=1)
def upcast_credit_decision_v1_to_v2(payload: dict) -> dict:
    return {
        **payload,
        "model_version": payload.get("model_version") or "legacy-pre-2026",
        "confidence_score": payload.get("confidence_score"),   # None for v1
        "regulatory_basis": payload.get("regulatory_basis") or infer_regulatory_basis(payload),
    }
```

For **CreditAnalysisCompleted** v1→v2 (catalogue event), the spec asks for `model_version`, `confidence_score`, `regulatory_basis`:

- **model_version:** For historical events we have no stored value. **Inference strategy:** use a sentinel string such as `"legacy-pre-2026"` (or derive from `recorded_at` if we have it in metadata, e.g. “if recorded_at < 2026-01-01 then model_version = 'legacy-pre-2026'”). We **do not** fabricate a specific version number (e.g. "2.1.0") because that would imply we know which model was used; that would be misleading for audit and model-attribution.
- **confidence_score:** **Inference strategy:** set to `None`. We do not invent a number. Fabricating a confidence score would be worse than null because: (a) regulators could treat it as real data; (b) analytics (e.g. “average confidence over time”) would be polluted; (c) “unknown” is the honest representation. Downstream code must handle `confidence_score is None` (e.g. exclude from confidence-based rules or show “Not recorded”).
- **regulatory_basis:** **Inference strategy:** if we have a way to know which regulation set was active at event time (e.g. from `recorded_at` and a regulation version table), we can set `regulatory_basis` to that set’s identifier. If not, use a sentinel like `"unknown-pre-2026"` or `None`. Again we do not invent a specific rule list.

**Immutability:** The upcaster runs **at read time** only. The raw payload in the `events` table is **never updated**. So when we load an old event, we get v1 from the DB and return v2 in memory; the stored row remains v1.

---

## 6. The Marten Async Daemon parallel

**Question:** Marten 7.0 introduced distributed projection execution across multiple nodes. Describe how you would achieve the same pattern in your Python implementation. What coordination primitive do you use, and what failure mode does it guard against?

**Answer:**

**Goal:** Only one node should process a given projection’s batch at a time, so we do not have two nodes both applying the same events to the same read model (which would cause double application, corrupted state, or non-idempotent updates).

**Coordination primitive:**

- **Option A — Per-projection lock:** Before processing a batch for projection P, the daemon acquires a **distributed lock** keyed by projection name (e.g. `ledger:projection:ApplicationSummary`). Use **Redis** with `SET key value NX PX ttl` (or Redlock for multi-instance Redis), or **PostgreSQL** with `pg_try_advisory_lock(projection_name_hash)` and a short lease (e.g. 30s). The daemon holds the lock for the duration of one batch, then releases and sleeps until the next poll. If another node holds the lock, this node skips this cycle and retries later.
- **Option B — Leader election:** One leader per projection (e.g. via etcd/Consul or a “leader” row in the database). Only the leader runs the projection daemon for that projection. Simpler mental model but adds a separate subsystem.

**Recommended for this stack:** **PostgreSQL advisory lock** keyed by projection name. No extra infrastructure; same DB as the event store. Example: `SELECT pg_try_advisory_xact_lock(hashtext('ApplicationSummary'))` at the start of the batch; if it returns false, skip and retry next poll. Use a short batch window so the lock is held for hundreds of ms, not minutes.

**Failure mode guarded against:**

- **Double application of the same event** by two nodes. Without coordination, Node A and Node B could both read `last_position = 100`, both load events 101–200, both apply them to the ApplicationSummary table. Result: duplicate or conflicting updates (e.g. same application_id updated twice, or counters doubled). The lock ensures **only one node** runs the projection for a given name at a time, so there is a single writer to the read model per projection.

**Additional considerations:**

- **Checkpoint updates** must be in the same transaction as the projection writes (or at least atomic with them) so that after a crash we do not have applied events but an outdated checkpoint.
- **Lock TTL / heartbeat:** If a node holds the lock and crashes without releasing, we need a finite TTL or heartbeat so another node can take over after a short delay; with advisory locks we rely on the session (connection) dying and the lock being released when the connection drops.

---

*End of DOMAIN_NOTES.md. Implementation begins only after this document is complete and agreed.*
