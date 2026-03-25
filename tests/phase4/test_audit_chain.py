import json
import os

import asyncpg
import pytest

from src.event_store import EventStore
from src.models.events import ApplicationSubmitted
from src.integrity.audit_chain import run_integrity_check, verify_audit_chain


async def apply_schema(conn):
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "src", "schema.sql")) as f:
        await conn.execute(f.read())


@pytest.mark.asyncio
async def test_audit_chain_appends_check_event():
    dsn = os.environ.get("LEDGER_TEST_DSN", "postgresql://postgres:postgres@localhost:5432/ledger_test")
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")

    await apply_schema(conn)
    store = EventStore(conn)

    app_id = "audit-1"
    await store.append(
        f"loan-{app_id}",
        [ApplicationSubmitted(
            application_id=app_id,
            applicant_id="u1",
            requested_amount_usd=5000.0,
            loan_purpose="ops",
            submission_channel="api",
            submitted_at="2026-01-01T00:00:00Z",
        )],
        expected_version=-1,
    )

    result = await run_integrity_check(store, "loan", app_id)
    assert result.chain_valid is True
    assert result.events_verified == 1

    audit_events = await store.load_stream(f"audit-loan-{app_id}")
    assert len(audit_events) == 1
    assert audit_events[0].event_type == "AuditIntegrityCheckRun"

    v = await verify_audit_chain(store, "loan", app_id)
    assert v.chain_valid is True
    assert v.tamper_detected is False

    await conn.close()


@pytest.mark.asyncio
async def test_audit_chain_detects_primary_tampering():
    dsn = os.environ.get("LEDGER_TEST_DSN", "postgresql://postgres:postgres@localhost:5432/ledger_test")
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")

    await apply_schema(conn)
    store = EventStore(conn)

    app_id = "audit-tamper"
    await store.append(
        f"loan-{app_id}",
        [ApplicationSubmitted(
            application_id=app_id,
            applicant_id="u1",
            requested_amount_usd=5000.0,
            loan_purpose="ops",
            submission_channel="api",
            submitted_at="2026-01-01T00:00:00Z",
        )],
        expected_version=-1,
    )

    await run_integrity_check(store, "loan", app_id)

    bad = {"application_id": app_id, "applicant_id": "tampered", "requested_amount_usd": 999.0, "loan_purpose": "ops", "submission_channel": "api", "submitted_at": "2026-01-01T00:00:00Z"}
    await conn.execute(
        "UPDATE events SET payload = $1::jsonb WHERE stream_id = $2",
        json.dumps(bad),
        f"loan-{app_id}",
    )

    v = await verify_audit_chain(store, "loan", app_id)
    assert v.chain_valid is False
    assert v.tamper_detected is True

    await conn.close()
