"""Single-threaded behaviour of the gateway store: T1, claims, fences, sweeper, segments, terminal states."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from raeg_gateway.audit import check_invariants
from raeg_gateway.store import ExpiredFence, OwnerFence
from raeg_gateway.transitions import Basis, IllegalTransition, State

from .conftest import PAYLOAD, submit


async def assert_clean(admin) -> None:
    report = await check_invariants(admin)
    assert {k: v for k, v in report.items() if v} == {}


async def test_t1_created_duplicate_conflict(store, admin) -> None:
    key, r1 = await submit(store)
    _, r2 = await submit(store, key)
    _, r3 = await submit(store, key, payload={**PAYLOAD, "amount_minor": 1}, fp="fp-other")
    assert (r1.outcome, r2.outcome, r3.outcome) == ("created", "duplicate", "fingerprint_conflict")
    assert r2.state == r3.state == State.PREPARED and r2.version == 1
    outcomes = [r["outcome"] for r in await admin.fetch("SELECT outcome FROM submissions WHERE action_key=$1 ORDER BY id", key)]
    assert outcomes == ["created", "duplicate", "fingerprint_conflict"]
    assert await admin.fetchval("SELECT count(*) FROM execution_events WHERE action_key=$1", key) == 1
    await assert_clean(admin)


async def test_happy_path_and_write_ahead(store, admin) -> None:
    key, _ = await submit(store)
    claim = await store.claim(key, worker_id="w1", attempt_ms=2000, basis=Basis.INITIAL)
    assert claim.applied and claim.attempt_no == 1
    snap = await store.snapshot(key)
    assert snap["state"] == "DISPATCHING" and snap["lease_owner"] == "w1"
    assert snap["lease_expires_at"] == claim.deadline == snap["max_deadline"]
    assert snap["anchor"] == claim.deadline - timedelta(milliseconds=2000)
    attempt = await admin.fetchrow("SELECT * FROM attempts WHERE action_key=$1", key)
    assert attempt["deadline"] == claim.deadline and attempt["basis"] == "INITIAL"
    ev = await store.append_evidence(key, kind="dispatch_response", worker_id="w1", attempt_no=1, http_status=201,
                                     raw_body=b'{"status":"succeeded"}', classification="COMMITTED")
    done = await store.transition(key, "T3", worker_id="w1", rule_id="RESP_COMMITTED", evidence_ids=[ev],
                                  patch={"provider_txn_id": "pay_1", "outcome_source": "response"},
                                  fence=OwnerFence("w1", claim.lease_token, 1))
    assert done.applied and done.state == State.SUCCEEDED and done.version == 3
    snap = await store.snapshot(key)
    assert snap["lease_owner"] is None and snap["lease_expires_at"] is None
    event = await admin.fetchrow("SELECT * FROM execution_events WHERE action_key=$1 AND seq=3", key)
    assert list(event["evidence_ids"]) == [ev]
    await assert_clean(admin)


async def test_illegal_transitions_are_refused_before_any_write(store, admin) -> None:
    key, _ = await submit(store)
    with pytest.raises(IllegalTransition):
        await store.transition(key, "T3", worker_id="w", rule_id="x")  # PREPARED -> SUCCEEDED
    with pytest.raises(IllegalTransition):
        await store.transition(key, "T2", worker_id="w", rule_id="x")  # claims only via claim()
    claim = await store.claim(key, worker_id="w", attempt_ms=2000, basis=Basis.INITIAL)
    with pytest.raises(IllegalTransition):
        await store.transition(key, "T5", worker_id="w", rule_id="x")  # leaving a lease needs a fence
    with pytest.raises(IllegalTransition):
        await store.transition(key, "T5", worker_id="w", rule_id="x", patch={"lease_owner": "evil"},
                               fence=OwnerFence("w", claim.lease_token))
    with pytest.raises(IllegalTransition):
        await store.transition(key, "T5", worker_id="w", rule_id="x", patch={"fingerprint": "evil"},
                               fence=OwnerFence("w", claim.lease_token))
    assert (await store.snapshot(key))["version"] == 2
    await assert_clean(admin)


async def test_owner_fence_rejects_stale_holders(store, admin) -> None:
    key, _ = await submit(store)
    claim = await store.claim(key, worker_id="w1", attempt_ms=2000, basis=Basis.INITIAL)
    for fence in (OwnerFence("w2", claim.lease_token, 1), OwnerFence("w1", claim.lease_token + timedelta(seconds=1), 1),
                  OwnerFence("w1", claim.lease_token, 2)):
        r = await store.transition(key, "T3", worker_id=fence.worker_id, rule_id="RESP", fence=fence)
        assert not r.applied and r.reason == "FENCED"
    rejected = await admin.fetchval("SELECT count(*) FROM decisions WHERE action_key=$1 AND NOT applied", key)
    assert rejected == 3, "rejected decisions are kept for audit"
    assert (await store.snapshot(key))["state"] == "DISPATCHING"
    await assert_clean(admin)


async def test_sweeper_expires_leases_and_fences_late_owner(store, admin) -> None:
    key, _ = await submit(store)
    keep, _ = await submit(store)
    claim = await store.claim(key, worker_id="w1", attempt_ms=50, basis=Basis.INITIAL)
    await store.claim(keep, worker_id="w1", attempt_ms=60_000, basis=Basis.INITIAL)
    await asyncio.sleep(0.15)
    swept = await store.sweep_expired("sweeper")
    assert [r.state for r in swept] == [State.UNKNOWN]
    snap = await store.snapshot(key)
    assert snap["state"] == "UNKNOWN" and snap["ambiguity_cause"] == "LEASE_EXPIRED"
    assert (await store.snapshot(keep))["state"] == "DISPATCHING", "unexpired leases are untouched"
    late = await store.transition(key, "T3", worker_id="w1", rule_id="RESP", fence=OwnerFence("w1", claim.lease_token, 1))
    assert not late.applied and late.reason == "FENCED"
    assert (await store.snapshot(key))["state"] == "UNKNOWN"
    assert await store.sweep_expired("sweeper") == [], "nothing left to sweep"
    await assert_clean(admin)


async def test_expired_fence_refuses_live_lease(store, admin) -> None:
    key, _ = await submit(store)
    await store.claim(key, worker_id="w1", attempt_ms=60_000, basis=Basis.INITIAL)
    r = await store.transition(key, "T5", worker_id="sweeper", rule_id="SWEEP", fence=ExpiredFence())
    assert not r.applied and r.reason == "LEASE_NOT_EXPIRED"
    await assert_clean(admin)


async def test_segments_anchor_and_max_deadline(store, admin) -> None:
    key, _ = await submit(store)
    c1 = await store.claim(key, worker_id="w", attempt_ms=1000, basis=Basis.INITIAL)
    await store.transition(key, "T5", worker_id="w", rule_id="AMBIG", fence=OwnerFence("w", c1.lease_token, 1))
    await store.transition(key, "T12", worker_id="w", rule_id="R-RI", patch={"retry_basis": "IDEMPOTENT_REPLAY"})
    c2 = await store.claim(key, worker_id="w", attempt_ms=3000, basis=Basis.IDEMPOTENT_REPLAY)
    s2 = await store.snapshot(key)
    assert s2["segment_no"] == 1 and s2["anchor"] == c1.deadline - timedelta(milliseconds=1000), "replay keeps the segment"
    assert s2["max_deadline"] == c2.deadline
    await store.transition(key, "T5", worker_id="w", rule_id="AMBIG", fence=OwnerFence("w", c2.lease_token, 2))
    v = await store.transition(key, "T6", worker_id="w", rule_id="R-V", lease_ms=5000, patch={"verify_attempts": 1})
    lease = (await store.snapshot(key))["lease_expires_at"]
    await store.transition(key, "T10", worker_id="w", rule_id="L-RA", fence=OwnerFence("w", lease),
                           patch={"retry_basis": "VERIFIED_ABSENT", "absence_proven_through": s2["max_deadline"]})
    c3 = await store.claim(key, worker_id="w", attempt_ms=500, basis=Basis.VERIFIED_ABSENT)
    s3 = await store.snapshot(key)
    assert v.applied and s3["segment_no"] == 2 and s3["anchor"] == c3.deadline - timedelta(milliseconds=500)
    assert s3["max_deadline"] == max(c2.deadline, c3.deadline), "max_deadline never decreases"
    assert [r["segment_no"] for r in await admin.fetch("SELECT segment_no FROM attempts WHERE action_key=$1 ORDER BY attempt_no", key)] == [1, 1, 2]
    await assert_clean(admin)


async def test_claim_guard_failure_is_t13a_and_not_due_is_refused(store, admin) -> None:
    key, _ = await submit(store)
    c1 = await store.claim(key, worker_id="w", attempt_ms=1000, basis=Basis.INITIAL)
    await store.transition(key, "T5", worker_id="w", rule_id="AMBIG", fence=OwnerFence("w", c1.lease_token, 1))
    now = await admin.fetchval("SELECT now()")
    await store.transition(key, "T12", worker_id="w", rule_id="R-RI",
                           patch={"retry_basis": "IDEMPOTENT_REPLAY", "next_action_at": now + timedelta(seconds=30)})
    not_due = await store.claim(key, worker_id="w", attempt_ms=1000, basis=Basis.IDEMPOTENT_REPLAY)
    assert not not_due.applied and not_due.reason == "NOT_DUE"
    key2, _ = await submit(store)
    c = await store.claim(key2, worker_id="w", attempt_ms=1000, basis=Basis.INITIAL)
    await store.transition(key2, "T5", worker_id="w", rule_id="AMBIG", fence=OwnerFence("w", c.lease_token, 1))
    await store.transition(key2, "T12", worker_id="w", rule_id="R-RI", patch={"retry_basis": "IDEMPOTENT_REPLAY"})
    failed = await store.claim(key2, worker_id="w", attempt_ms=1000, basis=Basis.IDEMPOTENT_REPLAY,
                               guard=lambda snap, now, d: (False, "TTL_GUARD", "d + eps + m > anchor + tau"))
    assert not failed.applied and failed.reason == "GUARD_FAILED"
    snap = await store.snapshot(key2)
    assert snap["state"] == "UNKNOWN" and snap["ambiguity_cause"] == "RETRY_GUARD_FAILED:TTL_GUARD"
    assert await admin.fetchval("SELECT count(*) FROM attempts WHERE action_key=$1", key2) == 1, "no attempt row on guard failure"
    last = await admin.fetchrow("SELECT transition FROM execution_events WHERE action_key=$1 ORDER BY seq DESC LIMIT 1", key2)
    assert last["transition"] == "T13a"
    await assert_clean(admin)


async def test_terminal_states_are_absorbing_and_escalation_is_operator_only(store, admin) -> None:
    key, _ = await submit(store)
    c = await store.claim(key, worker_id="w", attempt_ms=1000, basis=Basis.INITIAL)
    await store.transition(key, "T4", worker_id="w", rule_id="DD", fence=OwnerFence("w", c.lease_token, 1))
    for tid in ("T5", "T6", "T7", "T12", "T14", "T15", "T16s"):
        with pytest.raises(IllegalTransition):
            await store.transition(key, tid, worker_id="w", rule_id="x")
    key2, _ = await submit(store)
    c = await store.claim(key2, worker_id="w", attempt_ms=1000, basis=Basis.INITIAL)
    await store.transition(key2, "T5", worker_id="w", rule_id="AMBIG", fence=OwnerFence("w", c.lease_token, 1))
    await store.transition(key2, "T14", worker_id="w", rule_id="R-E", patch={"escalation_reason": "NO_SAFE_BASIS"})
    for tid in ("T6", "T12", "T15"):
        with pytest.raises(IllegalTransition):
            await store.transition(key2, tid, worker_id="w", rule_id="x")
    ev = await store.append_evidence(key2, kind="operator_note", worker_id="operator-1", request={"note": "verified manually"})
    r = await store.transition(key2, "T16s", worker_id="operator-1", rule_id="OPERATOR", evidence_ids=[ev],
                               patch={"outcome_source": "operator"})
    assert r.applied and r.state == State.SUCCEEDED
    await assert_clean(admin)


async def test_anomaly_and_fenced_evidence_are_recorded(store, admin) -> None:
    key, _ = await submit(store)
    ev = await store.append_evidence(key, kind="late_response", worker_id="zombie", attempt_no=1, http_status=201, fenced=True)
    an = await store.append_anomaly(key, "CONTRADICTORY_EVIDENCE", [ev], {"note": "test"})
    row = await admin.fetchrow("SELECT * FROM evidence WHERE id=$1", ev)
    assert row["fenced"] and len(row["sha256"]) == 64
    assert await admin.fetchval("SELECT kind FROM anomalies WHERE id=$1", an) == "CONTRADICTORY_EVIDENCE"


async def test_same_state_transitions_are_whitelisted_and_audited(store, admin) -> None:
    """TW (UNKNOWN→UNKNOWN) may only schedule; TA (SUCCEEDED→SUCCEEDED) may only annotate (N17/N18)."""
    key, _ = await submit(store)
    c = await store.claim(key, worker_id="w", attempt_ms=1000, basis=Basis.INITIAL)
    await store.transition(key, "T5", worker_id="w", rule_id="AMBIG", fence=OwnerFence("w", c.lease_token, 1))
    now = await admin.fetchval("SELECT now()")
    with pytest.raises(IllegalTransition):
        await store.transition(key, "TW", worker_id="w", rule_id="R-V-VB1-WAIT", patch={"escalation_reason": "x"})
    r = await store.transition(key, "TW", worker_id="w", rule_id="R-V-VB1-WAIT", patch={"next_action_at": now + timedelta(seconds=1)})
    assert r.applied and r.state == State.UNKNOWN
    with pytest.raises(IllegalTransition):
        await store.transition(key, "TA", worker_id="w", rule_id="x", patch={"duplicate_detected": True})  # not SUCCEEDED
    await store.transition(key, "T15", worker_id="w", rule_id="LS-OK", patch={"provider_txn_id": "p1"})
    with pytest.raises(IllegalTransition):
        await store.transition(key, "TA", worker_id="w", rule_id="x", patch={"provider_txn_id": "other"})
    r = await store.transition(key, "TA", worker_id="w", rule_id="LS-DUPLICATE", patch={"duplicate_detected": True})
    assert r.applied and (await store.snapshot(key))["duplicate_detected"] is True
    await assert_clean(admin)


async def test_stale_version_is_recorded_rejection_not_exception(store, admin) -> None:
    """N22: a CAS loser whose snapshot is stale gets VERSION_MISMATCH even if the move is now illegal."""
    key, _ = await submit(store)
    c = await store.claim(key, worker_id="w", attempt_ms=1000, basis=Basis.INITIAL)
    await store.transition(key, "T5", worker_id="w", rule_id="AMBIG", fence=OwnerFence("w", c.lease_token, 1))
    v = (await store.snapshot(key))["version"]
    await store.transition(key, "T6", worker_id="a", rule_id="R-V", lease_ms=5000, expected_version=v)
    loser = await store.transition(key, "T6", worker_id="b", rule_id="R-V", lease_ms=5000, expected_version=v)
    assert not loser.applied and loser.reason == "VERSION_MISMATCH"
    await assert_clean(admin)


async def test_sweeper_scoped_to_its_system(store, admin) -> None:
    """N32: a system's sweeper must not touch another system's leases."""
    from .conftest import CAPABILITY, PAYLOAD

    mine = "a-mine-" + str(id(store))
    other = "a-other-" + str(id(store))
    for key, sid in ((mine, "SYS-A"), (other, "SYS-B")):
        await store.submit(key, PAYLOAD, "fp", "P1", CAPABILITY, {"system_id": sid}, "w")
        await store.claim(key, worker_id="w", attempt_ms=20, basis=Basis.INITIAL)
    await asyncio.sleep(0.1)
    swept = await store.sweep_expired("sweeper-A", system_id="SYS-A")
    assert len(swept) == 1
    assert (await store.snapshot(mine))["state"] == "UNKNOWN"
    assert (await store.snapshot(other))["state"] == "DISPATCHING"
    await assert_clean(admin)
