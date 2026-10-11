"""Real-process crashes at CP1/CP2/CP3, zombie workers (F7), and concurrent engines.

The crashing/zombie worker is a separate OS process (raeg_gateway.worker_cli) whose crash hook calls
os._exit(137). Recovery runs in this test process under a different worker id.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from raeg_common.exchange_log import ExchangeLog
from raeg_common.provider_client import ProviderClient
from raeg_gateway.controller import Policy
from raeg_gateway.engine import RecoveryEngine
from raeg_gateway.transitions import TERMINAL, State
from tests.conformance.conftest import PUBLIC_URL, profile

from .conftest import PAYLOAD, assert_audit_clean, assert_decisions_recorded, outcome, run_worker_process


async def prepare(provider, engine, pid: str, policy: Policy, rules=None) -> str:
    key = f"cz-{uuid.uuid4().hex[:10]}"
    prof = profile(pid)
    await provider.configure(prof, [{**r, "client_reference": key} for r in (rules or [])])
    await engine.submit(key, PAYLOAD, pid, prof.capability, policy)
    return key


def hook_events(tmp_path, worker_id: str) -> list[dict]:
    path = tmp_path / f"exchanges-{worker_id}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("pid", ["P1", "P4"])
@pytest.mark.parametrize("hook", [{"point": "CP1"}, {"point": "CP2", "after_send_ms": 5}, {"point": "CP3"}],
                         ids=["CP1", "CP2", "CP3"])
async def test_real_process_crash_then_recovery(provider, store, engine, admin, tmp_path, pid, hook) -> None:
    key = await prepare(provider, engine, pid, Policy())
    proc = run_worker_process(key, [{**hook, "attempt_no": 1}], tmp_path)
    assert proc.returncode == 137, f"worker should die at {hook['point']}: rc={proc.returncode} out={proc.stdout}{proc.stderr}"
    events = hook_events(tmp_path, "crashing-worker")
    assert events and events[-1]["event"] == "hook_fired" and events[-1]["point"] == hook["point"]
    snap = await store.snapshot(key)
    assert snap["state"] == "DISPATCHING" and snap["lease_owner"] == "crashing-worker", "crash left the lease behind"

    final = await engine.run_to_quiescence(key, max_steps=400, poll_s=0.05)
    o = await outcome(provider, store, key)
    assert final == State.SUCCEEDED and o.effects == 1, (final, o)
    rules = o.rules
    assert "SWEEP_LEASE_EXPIRED" in rules
    if hook["point"] == "CP1":  # nothing was sent: absence must be proven before the retry
        assert o.attempts == 2 and "L-RA" in rules
    if hook["point"] == "CP3":  # committed: found by lookup, no re-dispatch
        assert o.attempts == 1 and rules[-1] == "L-S"
    await assert_audit_clean(admin)
    await assert_decisions_recorded(admin)


async def _zombie(provider, store, engine, admin, tmp_path, pid: str, policy: Policy):
    key = await prepare(provider, engine, pid, policy)
    zombie = run_worker_process(key, [{"point": "PAUSE", "pause_ms": 5000, "attempt_no": 1}], tmp_path,
                                worker_id="zombie-worker", wait=False)
    for _ in range(100):  # wait until the zombie holds the claim
        if (await store.snapshot(key))["lease_owner"] == "zombie-worker":
            break
        await asyncio.sleep(0.05)
    assert (await store.snapshot(key))["lease_owner"] == "zombie-worker"
    await engine.run_to_quiescence(key, max_steps=400, poll_s=0.05)
    out, _ = await asyncio.to_thread(zombie.communicate, timeout=60)
    assert zombie.returncode == 0, out
    await asyncio.sleep(0.2)
    await engine.consume_late_evidence()
    anomalies = [r["kind"] for r in await admin.fetch("SELECT kind FROM anomalies WHERE action_key=$1", key)]
    return key, await outcome(provider, store, key), anomalies


async def test_zombie_tier_s_deadline_fences_the_late_send(provider, store, engine, admin, tmp_path) -> None:
    """P4 (D): recovery proves absence after H+δ and retries; the zombie's late send is rejected by D."""
    key, o, anomalies = await _zombie(provider, store, engine, admin, tmp_path, "P4", Policy())
    assert (o.state, o.effects, o.attempts) == (State.SUCCEEDED, 1, 2)
    zombie_receipts = [r for r in await provider.receipts(key) if r["attempt_hdr"] == "1"]
    assert [r["disposition"] for r in zombie_receipts] == ["deadline_rejected_at_receipt"]
    late = await store.evidence_rows(key, "late_response")
    assert [e["classification"] for e in late] == ["DEADLINE_REJECTED"] and late[0]["fenced"]
    assert "LS-NOT-SUCCESS" in [r["rule_id"] for r in await admin.fetch("SELECT rule_id FROM decisions WHERE action_key=$1", key)]
    assert anomalies == []
    await assert_audit_clean(admin)


async def test_zombie_tier_s_strict_without_d_escalates_and_flags_late_success(provider, store, engine, admin, tmp_path) -> None:
    """P4n strict: absence is unprovable without D, so RAEG escalates; the zombie then commits once."""
    key, o, anomalies = await _zombie(provider, store, engine, admin, tmp_path, "P4n", Policy(mode="STRICT"))
    assert (o.state, o.effects) == (State.ESCALATED, 1)
    assert o.row["escalation_reason"] == "ABSENCE_UNPROVABLE"
    assert anomalies == ["LATE_SUCCESS_FOR_ESCALATED"]
    await assert_audit_clean(admin)


async def test_zombie_violating_b1_in_bounded_mode_is_detected(provider, store, engine, admin, tmp_path) -> None:
    """Assumption-violation check (Tier B, not Tier S): a 5 s pause breaks B1 on P4n, so a duplicate is
    expected. The test asserts that the duplicate is DETECTED and recorded, not that it is prevented."""
    key, o, anomalies = await _zombie(provider, store, engine, admin, tmp_path, "P4n", Policy(mode="BOUNDED"))
    assert o.state == State.SUCCEEDED and o.effects == 2, "B1 violated → duplicate (expected)"
    assert o.row["duplicate_detected"] is True
    assert anomalies == ["DUPLICATE_DETECTED"]
    assert "LS-DUPLICATE" in [r["rule_id"] for r in await admin.fetch("SELECT rule_id FROM decisions WHERE action_key=$1", key)]
    await assert_audit_clean(admin)


async def test_concurrent_engines_process_shared_backlog_exactly_once(provider, store, admin, tmp_path) -> None:
    keys = [f"cc-{uuid.uuid4().hex[:8]}" for _ in range(20)]
    rules = [{"client_reference": k, "ingress_index": 1, "fault": "LOST_RESPONSE", "params": {}} for k in keys[::2]]
    prof = profile("P1")
    await provider.configure(prof, rules)
    clients, engines = [], []
    for i in range(3):
        client = ProviderClient(PUBLIC_URL, f"raeg-cc-{i}", timeout_s=10, log=ExchangeLog(str(tmp_path / f"cc{i}.jsonl")))
        clients.append(client)
        engines.append(RecoveryEngine(store, client, f"cc-engine-{i}"))
    for k in keys:
        await engines[0].submit(k, PAYLOAD, "P1", prof.capability, Policy())

    async def loop(e: RecoveryEngine) -> None:
        for _ in range(400):
            await e.run_once(limit=50)
            rows = await store.pool.fetch("SELECT state FROM executions WHERE action_key = ANY($1)", keys)
            if all(State(r["state"]) in TERMINAL or r["state"] == "ESCALATED" for r in rows):
                return
            await asyncio.sleep(0.05)

    await asyncio.gather(*(loop(e) for e in engines))
    for c in clients:
        await c.aclose()
    for k in keys:
        o = await outcome(provider, store, k)
        assert (o.state, o.effects, o.attempts) == (State.SUCCEEDED, 1, 1), (k, o)
    claims = await admin.fetchval("SELECT count(*) FROM decisions WHERE transition IN ('T2','T13') AND applied")
    assert claims == len(keys), "exactly one dispatch claim per action across three engines"
    await assert_audit_clean(admin)
    await assert_decisions_recorded(admin)
