"""Append-only enforcement (privileges + triggers), tamper detection, projection rebuild (I8)."""

from __future__ import annotations

import asyncpg
import pytest

from raeg_gateway.audit import compare_projection, rebuild_projection, verify_chains
from raeg_gateway.store import OwnerFence
from raeg_gateway.transitions import Basis

from .conftest import submit

APPEND_ONLY = ["submissions", "attempts", "evidence", "decisions", "execution_events", "anomalies"]


async def populate(store) -> str:
    key, _ = await submit(store)
    c = await store.claim(key, worker_id="w", attempt_ms=1000, basis=Basis.INITIAL)
    ev = await store.append_evidence(key, kind="transport_error", worker_id="w", attempt_no=1, classification="AMBIGUOUS")
    await store.transition(key, "T5", worker_id="w", rule_id="AMBIG", evidence_ids=[ev], fence=OwnerFence("w", c.lease_token, 1),
                           patch={"ambiguity_cause": "ReadTimeout"})
    await store.append_anomaly(key, "TEST", [ev])
    return key


@pytest.mark.parametrize("table", APPEND_ONLY)
@pytest.mark.parametrize("statement", ["UPDATE {t} SET action_key = action_key", "DELETE FROM {t}", "TRUNCATE {t} CASCADE"])
async def test_app_role_cannot_mutate_evidence(store, app_conn, table: str, statement: str) -> None:
    await populate(store)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute(statement.format(t=table))


@pytest.mark.parametrize("statement", [
    "UPDATE executions SET fingerprint = 'x'",
    "UPDATE executions SET payload = '{}'",
    "UPDATE executions SET capability = '{}'",
    "DELETE FROM executions",
    "TRUNCATE executions CASCADE",
])
async def test_app_role_cannot_change_identity_or_delete_executions(store, app_conn, statement: str) -> None:
    await populate(store)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute(statement)


@pytest.mark.parametrize("table", APPEND_ONLY)
async def test_triggers_bind_even_the_owner_role(store, admin, table: str) -> None:
    await populate(store)
    for statement in (f"UPDATE {table} SET action_key = action_key", f"DELETE FROM {table}"):
        with pytest.raises(asyncpg.PostgresError, match="append-only") as err:
            await admin.execute(statement)
        assert err.value.sqlstate == "RA002"


async def test_tampered_event_is_detected(store, admin) -> None:
    key = await populate(store)
    assert await verify_chains(admin) == []
    await admin.execute("ALTER TABLE execution_events DISABLE TRIGGER execution_events_append_only")
    try:
        await admin.execute(
            "UPDATE execution_events SET patch = jsonb_set(patch, '{ambiguity_cause}', '\"edited\"') WHERE action_key=$1 AND seq=3", key
        )
    finally:
        await admin.execute("ALTER TABLE execution_events ENABLE TRIGGER execution_events_append_only")
    problems = await verify_chains(admin)
    assert any("hash mismatch" in p and "seq 3" in p for p in problems)


async def test_deleted_event_breaks_the_chain(store, admin) -> None:
    key = await populate(store)
    await admin.execute("ALTER TABLE execution_events DISABLE TRIGGER execution_events_append_only")
    try:
        await admin.execute("DELETE FROM execution_events WHERE action_key=$1 AND seq=2", key)
    finally:
        await admin.execute("ALTER TABLE execution_events ENABLE TRIGGER execution_events_append_only")
    problems = await verify_chains(admin)
    assert any("seq 3 where 2 expected" in p for p in problems) and any("does not link" in p for p in problems)


async def test_projection_rebuild_matches_and_detects_drift(store, admin) -> None:
    keys = [await populate(store) for _ in range(5)]
    assert await compare_projection(admin) == []
    rebuilt = await rebuild_projection(admin)
    assert set(rebuilt) == set(keys)
    # Out-of-band edit of the projection (owner role) is detected as drift from the event log.
    await admin.execute("UPDATE executions SET ambiguity_cause = 'edited' WHERE action_key=$1", keys[0])
    problems = await compare_projection(admin)
    assert problems == [f"{keys[0]}.ambiguity_cause: table='edited' rebuilt='ReadTimeout'"]
