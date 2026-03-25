-- =============================================================================
-- The Ledger — Event Store Schema (Phase 1)
-- =============================================================================
-- Append-only events, per-stream metadata (optimistic concurrency cursor),
-- projection checkpoints, and transactional outbox for reliable downstream
-- delivery. Design goals: audit-grade immutability, referential integrity,
-- and indexes aligned to common read paths (by stream, global order, type,
-- time, and outbox polling).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- event_streams: one row per stream — aggregate type, version cursor, archive.
-- Created before events (events.stream_id references this table).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_streams (
  stream_id        TEXT PRIMARY KEY,
  -- Classifier for ops and routing (e.g. LoanApplication).
  aggregate_type   TEXT NOT NULL,
  -- Count of events appended; used with expected_version for optimistic locking.
  current_version  BIGINT NOT NULL DEFAULT 0,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  archived_at      TIMESTAMPTZ,
  -- Arbitrary stream-level metadata (tenant, tags).
  metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT chk_event_streams_version_nonneg CHECK (current_version >= 0)
);

COMMENT ON TABLE event_streams IS 'Per-stream metadata and concurrency version; pairs with events.stream_id.';
COMMENT ON COLUMN event_streams.current_version IS 'Next stream_position to assign = current_version (event count).';
COMMENT ON COLUMN event_streams.archived_at IS 'When set, appends should be rejected by application policy.';

CREATE INDEX IF NOT EXISTS idx_event_streams_aggregate ON event_streams (aggregate_type);
CREATE INDEX IF NOT EXISTS idx_event_streams_active ON event_streams (aggregate_type) WHERE archived_at IS NULL;

-- -----------------------------------------------------------------------------
-- events: append-only fact log. stream_position is 0-based per stream;
-- global_position is the monotonic identity for all-projectors / catch-up.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
  event_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  -- Logical partition: typically loan-{id}, agent-{agent}-{session}, etc.
  stream_id        TEXT NOT NULL,
  -- Per-stream sequence (0 .. N-1); paired with stream_id must be unique.
  stream_position  BIGINT NOT NULL,
  -- Monotonic global ordering for subscriptions and checkpointing.
  global_position  BIGINT GENERATED ALWAYS AS IDENTITY,
  -- Discriminator for deserialization (maps to Pydantic event class name).
  event_type       TEXT NOT NULL,
  -- Schema version for upcasting when event payloads evolve.
  event_version    SMALLINT NOT NULL DEFAULT 1,
  -- Domain payload (JSON); immutable after insert.
  payload          JSONB NOT NULL,
  -- Correlation/causation, tracing, actor id — audit and workflow context.
  metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
  -- Server-side timestamp at commit (not client-supplied).
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT uq_stream_position UNIQUE (stream_id, stream_position),
  CONSTRAINT chk_events_stream_position_nonneg CHECK (stream_position >= 0),
  CONSTRAINT fk_events_stream
    FOREIGN KEY (stream_id) REFERENCES event_streams (stream_id)
    ON DELETE RESTRICT
);

COMMENT ON TABLE events IS 'Append-only event log; each row is one domain event with server-assigned ordering.';
COMMENT ON COLUMN events.event_id IS 'Stable id for outbox and idempotent consumers.';
COMMENT ON COLUMN events.stream_id IS 'Aggregate / context stream key.';
COMMENT ON COLUMN events.stream_position IS '0-based index within stream; drives expected_version concurrency.';
COMMENT ON COLUMN events.global_position IS 'Cluster-wide sequence for projections and replay from position.';
COMMENT ON COLUMN events.event_type IS 'Maps to event class name for hydration.';
COMMENT ON COLUMN events.event_version IS 'Payload schema revision for migrations and upcasting.';
COMMENT ON COLUMN events.payload IS 'Serialized domain event body (JSON).';
COMMENT ON COLUMN events.metadata IS 'Correlation, causation, and audit metadata (JSON).';
COMMENT ON COLUMN events.recorded_at IS 'DB commit time for compliance timelines.';

-- Stream-scoped reads (replay, aggregate load).
CREATE INDEX IF NOT EXISTS idx_events_stream_id ON events (stream_id, stream_position);
-- Global log / catch-up from checkpoint.
CREATE INDEX IF NOT EXISTS idx_events_global_pos ON events (global_position);
-- Filter projections by type.
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type);
-- Time-range audit queries.
CREATE INDEX IF NOT EXISTS idx_events_recorded ON events (recorded_at);

-- -----------------------------------------------------------------------------
-- projection_checkpoints: projector resume positions.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projection_checkpoints (
  projection_name  TEXT PRIMARY KEY,
  last_position    BIGINT NOT NULL DEFAULT 0,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT chk_projection_checkpoint_nonneg CHECK (last_position >= 0)
);

COMMENT ON TABLE projection_checkpoints IS 'Last processed global_position per projection/builder.';

-- -----------------------------------------------------------------------------
-- outbox: same transaction as events — at-least-once publish to external systems.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbox (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id         UUID NOT NULL REFERENCES events (event_id) ON DELETE RESTRICT,
  destination      TEXT NOT NULL,
  payload          JSONB NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  published_at     TIMESTAMPTZ,
  attempts         SMALLINT NOT NULL DEFAULT 0,
  CONSTRAINT chk_outbox_attempts_nonneg CHECK (attempts >= 0)
);

COMMENT ON TABLE outbox IS 'Transactional outbox rows written in the same commit as events for reliable delivery.';
COMMENT ON COLUMN outbox.destination IS 'Logical sink (queue name, webhook id, etc.).';
COMMENT ON COLUMN outbox.published_at IS 'NULL until successfully published; supports retry workers.';

-- Workers: pick unpublished rows by destination, oldest first.
CREATE INDEX IF NOT EXISTS idx_outbox_pending
  ON outbox (destination, created_at)
  WHERE published_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_outbox_event ON outbox (event_id);

-- -----------------------------------------------------------------------------
-- Phase 3 projection read models
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS application_summary_projection (
  application_id            TEXT PRIMARY KEY,
  state                     TEXT NOT NULL,
  applicant_id              TEXT,
  requested_amount_usd      DOUBLE PRECISION,
  approved_amount_usd       DOUBLE PRECISION,
  risk_tier                 TEXT,
  fraud_score               DOUBLE PRECISION,
  compliance_status         TEXT,
  decision                  TEXT,
  agent_sessions_completed  JSONB NOT NULL DEFAULT '[]'::jsonb,
  last_event_type           TEXT,
  last_event_at             TIMESTAMPTZ,
  human_reviewer_id         TEXT,
  final_decision_at         TIMESTAMPTZ,
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_app_summary_state ON application_summary_projection (state);
CREATE INDEX IF NOT EXISTS idx_app_summary_updated ON application_summary_projection (updated_at);

CREATE TABLE IF NOT EXISTS agent_performance_projection (
  agent_id                  TEXT NOT NULL,
  model_version             TEXT NOT NULL,
  analyses_completed        BIGINT NOT NULL DEFAULT 0,
  decisions_generated       BIGINT NOT NULL DEFAULT 0,
  avg_confidence_score      DOUBLE PRECISION,
  avg_duration_ms           DOUBLE PRECISION,
  approve_rate              DOUBLE PRECISION,
  decline_rate              DOUBLE PRECISION,
  refer_rate                DOUBLE PRECISION,
  human_override_rate       DOUBLE PRECISION,
  first_seen_at             TIMESTAMPTZ,
  last_seen_at              TIMESTAMPTZ,
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (agent_id, model_version)
);
CREATE INDEX IF NOT EXISTS idx_agent_perf_updated ON agent_performance_projection (updated_at);

CREATE TABLE IF NOT EXISTS compliance_audit_projection (
  application_id            TEXT NOT NULL,
  as_of_event_position      BIGINT NOT NULL,
  as_of_recorded_at         TIMESTAMPTZ NOT NULL,
  regulation_set_version    TEXT,
  checks_required           JSONB NOT NULL DEFAULT '[]'::jsonb,
  passed_rules              JSONB NOT NULL DEFAULT '[]'::jsonb,
  failed_rules              JSONB NOT NULL DEFAULT '[]'::jsonb,
  status                    TEXT NOT NULL,
  latest_event_type         TEXT NOT NULL,
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (application_id, as_of_event_position)
);
CREATE INDEX IF NOT EXISTS idx_compliance_audit_time
  ON compliance_audit_projection (application_id, as_of_recorded_at DESC);

CREATE TABLE IF NOT EXISTS projection_snapshots (
  id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  projection_name           TEXT NOT NULL,
  entity_id                 TEXT NOT NULL,
  snapshot_position         BIGINT NOT NULL,
  snapshot_payload          JSONB NOT NULL,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_snapshot_entity_pos UNIQUE (projection_name, entity_id, snapshot_position)
);
CREATE INDEX IF NOT EXISTS idx_projection_snapshots_lookup
  ON projection_snapshots (projection_name, entity_id, snapshot_position DESC);
