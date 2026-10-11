"""Concurrency safety of the store (DESIGN §4): submission races, claim races, sweeper races,
owner-vs-sweeper races, and a randomized multi-worker stress run audited against I2/I5/I6/I8."""

from __future__ import annotations

import asyncio
import random

from raeg_gateway.audit import check_invariants
from raeg_gateway.store import OwnerFence
from raeg_gateway.transitions import LEASED, Basis, IllegalTransition, State

from .conftest import PAYLOAD, new_key, submit


async def assert_clean(admin) -> None:
    report = await check_invariants(admin)
    assert {k: v for k, v in report.items() if v} == {}


async def test_concurrent_identical_submissions_create_once(store, admin) -> None:
    key = new_key()
    results = await asyncio.gather(*(submit(store, key) for _ in range(50)))
    outcomes = sorted(r.outcome for _, r in results)
    assert outcomes.count("created") == 1 and outcomes.count("duplicate") == 49
    assert await admin.fetchval("SELECT count(*) FROM submissions WHERE action_key=$1", key) == 50
    assert await admin.fetchval("SELECT count(*) FROM execution_events WHERE action_key=$1", key) == 1
    await assert_clean(admin)


async def test_concurrent_conflicting_fingerprints(store, admin) -> None:
    key = new_key()
    variants = [("fp-A", PAYLOAD), ("fp-B", {**PAYLOAD, "amount_minor": 2})] * 20
    results = await asyncio.gather(*(submit(store, key, payload=p, fp=fp) for fp, p in variants))
    winner = await admin.fetchval("SELECT fingerprint FROM executions WHERE action_key=$1", key)
    created = [fp for (fp, _), (_, r) in zip(variants, results, strict=True) if r.outcome == "created"]
    assert created == [winner]
    for (fp, _), (_, r) in zip(variants, results, strict=True):
        if r.outcome != "created":
            assert r.outcome == ("duplicate" if fp == winner else "fingerprint_conflict")
    await assert_clean(admin)


async def test_concurrent_claims_grant_exactly_one_attempt(store, admin) -> None:
    keys = [(await submit(store))[0] for _ in range(10)]
    for key in keys:
        results = await asyncio.gather(
            *(store.claim(key, worker_id=f"w{i}", attempt_ms=5000, basis=Basis.INITIAL) for i in range(30))
        )
        winners = [r for r in results if r.applied]
        assert len(winners) == 1 and winners[0].attempt_no == 1
        assert all(r.reason == "STATE_MISMATCH" for r in results if not r.applied)
        assert await admin.fetchval("SELECT count(*) FROM attempts WHERE action_key=$1", key) == 1
    await assert_clean(admin)


async def test_concurrent_sweepers_expire_each_lease_once(store, admin) -> None:
    keys = [(await submit(store))[0] for _ in range(40)]
    for key in keys:
        await store.claim(key, worker_id="w", attempt_ms=20, basis=Basis.INITIAL)
    await asyncio.sleep(0.1)
    results = await asyncio.gather(*(store.sweep_expired(f"sweeper-{i}", limit=7) for i in range(12)))
    applied = [r for batch in results for r in batch if r.applied]
    assert len(applied) == 40
    assert await admin.fetchval("SELECT count(*) FROM execution_events WHERE transition='T5'") == 40
    await assert_clean(admin)


async def test_owner_completion_races_sweeper(store, admin) -> None:
    keys = [(await submit(store))[0] for _ in range(40)]
    claims = {}
    for key in keys:
        claims[key] = await store.claim(key, worker_id="owner", attempt_ms=30, basis=Basis.INITIAL)
    await asyncio.sleep(0.04)

    async def owner(key: str):
        return await store.transition(key, "T3", worker_id="owner", rule_id="RESP",
                                      fence=OwnerFence("owner", claims[key].lease_token, 1))

    async def sweeper(i: int):
        return await store.sweep_expired(f"sweeper-{i}", limit=50)

    tasks = [owner(k) for k in keys] + [sweeper(i) for i in range(4)]
    random.Random(7).shuffle(tasks)
    results = await asyncio.gather(*tasks)
    owner_applied = sum(1 for r in results if not isinstance(r, list) and r.applied)
    swept = sum(1 for r in results if isinstance(r, list) for x in r if x.applied)
    assert owner_applied + swept == 40, "every action left DISPATCHING exactly once"
    states = {r["state"] for r in await admin.fetch("SELECT state FROM executions")}
    assert states <= {"SUCCEEDED", "UNKNOWN"}
    await assert_clean(admin)


async def test_randomized_stress(store, admin) -> None:
    """Many workers apply random legal-looking operations with stale reads; the store must keep
    every invariant. Illegal requests (from stale snapshots) must be refused, never applied."""
    rng = random.Random(20261011)
    keys = [(await submit(store))[0] for _ in range(80)]
    held: dict[str, list] = {}  # key -> [(worker, lease_token, attempt_no)] grants seen by workers
    counters = {"applied": 0, "rejected": 0, "illegal": 0, "absorbing_refused": 0}

    async def op(worker: str, r: random.Random) -> None:
        key = r.choice(keys)
        snap = await store.snapshot(key)
        state = State(snap["state"])
        try:
            if state == State.PREPARED:
                res = await store.claim(key, worker_id=worker, attempt_ms=r.choice([5, 50, 2000]), basis=Basis.INITIAL)
                if res.applied:
                    held.setdefault(key, []).append((worker, res.lease_token, res.attempt_no))
            elif state == State.RETRY_WAIT:
                basis = Basis(snap["retry_basis"])
                guard_ok = r.random() < 0.8
                res = await store.claim(key, worker_id=worker, attempt_ms=r.choice([5, 50, 2000]), basis=basis,
                                        guard=lambda *_: (guard_ok, "G", "random"))
                if res.applied:
                    held.setdefault(key, []).append((worker, res.lease_token, res.attempt_no))
            elif state in LEASED:
                if r.random() < 0.3:
                    await store.sweep_expired(worker, limit=5)
                    return
                grants = held.get(key) or [(worker, snap["lease_expires_at"], snap["attempt_count"])]
                w, token, attempt = r.choice(grants)  # possibly a stale grant
                tid = r.choice(["T3", "T4", "T5"] if state == State.DISPATCHING else ["T7", "T8", "T9", "T10", "T11"])
                patch = {"retry_basis": "VERIFIED_ABSENT"} if tid == "T10" else {}
                res = await store.transition(key, tid, worker_id=w, rule_id="STRESS", patch=patch,
                                             fence=OwnerFence(w, token, attempt if state == State.DISPATCHING else None))
            elif state == State.UNKNOWN:
                tid = r.choice(["T6", "T12", "T14", "T15"])
                if tid == "T6":
                    res = await store.transition(key, tid, worker_id=worker, rule_id="STRESS", lease_ms=r.choice([5, 2000]),
                                                 expected_version=snap["version"])
                    if res.applied:
                        lease = (await store.snapshot(key))["lease_expires_at"]
                        held.setdefault(key, []).append((worker, lease, None))
                else:
                    patch = {"retry_basis": "IDEMPOTENT_REPLAY"} if tid == "T12" else {}
                    res = await store.transition(key, tid, worker_id=worker, rule_id="STRESS", patch=patch,
                                                 expected_version=snap["version"])
            elif state == State.ESCALATED:
                res = await store.transition(key, r.choice(["T16s", "T16d", "T16n"]), worker_id=worker, rule_id="OP",
                                             expected_version=snap["version"])
            else:  # absorbing: any request must be refused
                try:
                    await store.transition(key, r.choice(["T5", "T6", "T12", "T14"]), worker_id=worker, rule_id="STRESS")
                except IllegalTransition:
                    counters["absorbing_refused"] += 1
                    return
                raise AssertionError("transition out of an absorbing state was accepted")
            counters["applied" if res.applied else "rejected"] += 1
        except IllegalTransition:
            counters["illegal"] += 1  # stale snapshot asked for a now-illegal move: refused before any write

    async def worker(i: int) -> None:
        r = random.Random(rng.random())
        for _ in range(60):
            await op(f"w{i}", r)
            if r.random() < 0.1:
                await asyncio.sleep(0.01)

    await asyncio.gather(*(worker(i) for i in range(16)))
    await asyncio.sleep(0.06)
    await store.sweep_expired("final", limit=1000)
    # The run must actually exercise races and refusals, not just pass vacuously.
    assert counters["applied"] > 2 * len(keys), counters
    assert counters["rejected"] > 0 and counters["absorbing_refused"] > 0, counters
    print("stress counters:", counters)
    await assert_clean(admin)
    leased_left = await admin.fetchval("SELECT count(*) FROM executions WHERE state IN ('DISPATCHING','VERIFYING') AND lease_expires_at < now()")
    assert leased_left == 0, "the final sweep left no expired lease behind"
