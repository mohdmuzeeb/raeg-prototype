"""Each baseline implements its DESIGN §5.4 definition — checked with deterministic single faults.

These are implementation-conformance checks ("does B2 actually send the key and replay?"), not
comparative measurements: each scenario is one hand-picked action, chosen to exercise one clause
of a definition. Comparative results come only from the M8 experiments.
"""

from __future__ import annotations

import asyncio

from analysis.claims import normalize
from raeg_common.models import Claim

from .conftest import configure, new_key, rule, submit, ts

DELTA_MS = 500  # P4's declared lookup lag (config/profiles/P4.yaml)


async def run(systems, provider, sid: str, pid: str, rules: list[dict]) -> tuple[str, dict, list, list]:
    key = new_key(sid)
    await configure(provider, pid, key, rules)
    native = await submit(systems, sid, key, pid)
    await asyncio.sleep(0.3)
    return key, native, await provider.ledger(key), await provider.receipts(key)


# ----------------------------------------------------------------------------- B1 / B2


async def test_b1_resends_without_key(systems, provider) -> None:
    _, native, ledger, receipts = await run(systems, provider, "B1", "P1", [rule(1, "LOST_RESPONSE")])
    assert native["status"] == "ok" and native["detail"]["attempts"] == 2
    assert [r["idempotency_key"] for r in receipts] == [None, None]
    assert len(ledger) == 2, "definition: a naive re-send after a lost response is a second payment"


async def test_b2_resends_with_key_and_gets_replay(systems, provider) -> None:
    key, native, ledger, receipts = await run(systems, provider, "B2", "P1", [rule(1, "LOST_RESPONSE")])
    assert native["status"] == "ok" and native["detail"]["replayed"] is True
    assert [r["idempotency_key"] for r in receipts] == [key, key]
    assert len(ledger) == 1


async def test_b2_key_is_ignored_by_provider_without_idempotency(systems, provider) -> None:
    _, native, ledger, _ = await run(systems, provider, "B2", "P6", [rule(1, "LOST_RESPONSE")])
    assert native["status"] == "ok" and len(ledger) == 2


async def test_retry_baselines_give_up_with_error_after_budget(systems, provider) -> None:
    for sid in ("B1", "B2"):
        _, native, ledger, receipts = await run(systems, provider, sid, "P1", [rule(i, "ERROR_5XX_PRE") for i in (1, 2, 3)])
        assert native["status"] == "error" and native["detail"]["attempts"] == 3 and len(receipts) == 3
        assert normalize(native) == Claim.UNRESOLVED_GAVE_UP and ledger == []


async def test_retry_baselines_stop_on_decline(systems, provider) -> None:
    for sid in ("B1", "B2"):
        _, native, _, receipts = await run(systems, provider, sid, "P1", [rule(1, "FORCE_DECLINE")])
        assert native["status"] == "declined" and len(receipts) == 1
        assert normalize(native) == Claim.NO_EFFECT_DECLINED


async def test_b1_has_no_local_deduplication(systems, provider) -> None:
    key = new_key("B1")
    await configure(provider, "P1", key)
    natives = await asyncio.gather(*(submit(systems, "B1", key, "P1") for _ in range(3)))
    assert all(n["status"] == "ok" for n in natives)
    assert len(await provider.ledger(key)) == 3, "definition: every submission executes"


# ----------------------------------------------------------------------------- B3 (Temporal)


async def test_b3_activity_retry_reuses_key(systems, provider) -> None:
    key, native, ledger, receipts = await run(systems, provider, "B3", "P1", [rule(1, "LOST_RESPONSE")])
    assert native["status"] == "ok" and native["detail"]["replayed"] is True
    assert [r["attempt_hdr"] for r in receipts] == ["1", "2"] and {r["idempotency_key"] for r in receipts} == {key}
    assert len(ledger) == 1


async def test_b3_decline_is_non_retryable(systems, provider) -> None:
    _, native, _, receipts = await run(systems, provider, "B3", "P1", [rule(1, "FORCE_DECLINE")])
    assert native["status"] == "declined" and len(receipts) == 1


async def test_b3_retry_policy_exhausts_after_max_dispatch(systems, provider) -> None:
    _, native, ledger, receipts = await run(systems, provider, "B3", "P1", [rule(i, "ERROR_5XX_PRE") for i in (1, 2, 3)])
    assert native["status"] == "error" and len(receipts) == 3 and ledger == []
    assert normalize(native) == Claim.UNRESOLVED_GAVE_UP


async def test_b3_workflow_id_deduplicates_submissions(systems, provider) -> None:
    key = new_key("B3")
    await configure(provider, "P1", key)
    natives = await asyncio.gather(*(submit(systems, "B3", key, "P1") for _ in range(5)))
    assert {n["provider_txn_id"] for n in natives} != {None} and len({n["provider_txn_id"] for n in natives}) == 1
    again = await submit(systems, "B3", key, "P1")  # after completion: attaches to the existing result
    assert again["provider_txn_id"] == natives[0]["provider_txn_id"]
    assert len(await provider.receipts(key)) == 1 and len(await provider.ledger(key)) == 1


# ----------------------------------------------------------------------------- B3L


async def test_b3l_delta_waits_delta_then_redispatches_after_miss(systems, provider) -> None:
    key, native, ledger, receipts = await run(systems, provider, "B3L-delta", "P4", [rule(1, "PRE_COMMIT_STALL", stall_ms=2500)])
    lookups = await provider.receipts(key, kind="lookup_ref")
    assert native["status"] == "ok" and native["detail"]["found_by"] == "response" and native["detail"]["attempt_no"] == 2
    assert len(lookups) >= 1 and ts(lookups[0]["arrived_at"]) >= ts(receipts[0]["arrived_at"]) + _ms(DELTA_MS)
    assert len(ledger) == 1


async def test_b3l_horizon_waits_until_deadline_plus_delta(systems, provider) -> None:
    key, native, ledger, receipts = await run(systems, provider, "B3L-horizon", "P4", [rule(1, "PRE_COMMIT_STALL", stall_ms=2500)])
    lookups = await provider.receipts(key, kind="lookup_ref")
    assert native["status"] == "ok" and len(ledger) == 1
    d1 = ts(receipts[0]["deadline"])
    assert ts(lookups[0]["arrived_at"]) >= d1 + _ms(DELTA_MS), "lookup only after max(d_j) + δ"


async def test_b3l_finds_lost_response_without_redispatch(systems, provider) -> None:
    for sid in ("B3L-delta", "B3L-horizon"):
        _, native, ledger, receipts = await run(systems, provider, sid, "P4", [rule(1, "LOST_RESPONSE")])
        assert native["status"] == "ok" and native["detail"]["found_by"] == "lookup"
        assert len(receipts) == 1 and len(ledger) == 1


async def test_b3l_believes_misses_and_reports_not_found(systems, provider) -> None:
    _, native, ledger, receipts = await run(systems, provider, "B3L-horizon", "P4",
                                            [rule(i, "PRE_COMMIT_STALL", stall_ms=2500) for i in (1, 2, 3)])
    assert native["status"] == "not_found_after_retries" and len(receipts) == 3 and ledger == []
    assert normalize(native) == Claim.NO_EFFECT_VERIFIED


# ----------------------------------------------------------------------------- RAEG via the contract


async def test_raeg_concurrent_submissions_create_one_effect(systems, provider) -> None:
    key = new_key("RAEG-strict")
    await configure(provider, "P1", key)
    natives = await asyncio.gather(*(submit(systems, "RAEG-strict", key, "P1") for _ in range(5)))
    assert {n["status"] for n in natives} == {"ok"}
    assert len(await provider.receipts(key)) == 1 and len(await provider.ledger(key)) == 1


async def test_raeg_escalation_maps_to_unresolved(systems, provider) -> None:
    _, native, ledger, _ = await run(systems, provider, "RAEG-strict", "P6", [rule(1, "LOST_RESPONSE")])
    assert native["status"] == "escalated" and normalize(native) == Claim.UNRESOLVED_ESCALATED and len(ledger) == 1


def _ms(n: int):
    from datetime import timedelta

    return timedelta(milliseconds=n)
