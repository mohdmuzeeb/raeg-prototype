"""Lookup contract L1–L5 (DESIGN §2.5) and commit-time deadline D (§2.7)."""

from __future__ import annotations

import asyncio

import pytest

from .conftest import ms, profile, ts

LOOKUP_PROFILES = ["P1", "P2", "P4", "P4n"]
D_PROFILES = ["P1", "P2", "P4", "P7"]


@pytest.mark.parametrize("pid", LOOKUP_PROFILES)
async def test_l1_in_flight_payment_is_never_returned(provider, pid: str) -> None:
    rules = [{"client_reference": "l1", "ingress_index": 1, "fault": "DELAYED_COMMIT", "params": {"delay_ms": 1200}}]
    await provider.configure(profile(pid), rules)
    task = asyncio.create_task(provider.pay("l1"))
    await asyncio.sleep(0.5)
    during = await provider.lookup("l1")
    assert during.status_code == 200 and during.json()["records"] == []
    assert (await task).status == 201
    (payment,) = await provider.ledger("l1")
    assert ts(during.json()["as_of"]) < ts(payment["committed_at"]), "precondition: lookup was before the commit"


@pytest.mark.parametrize("pid", LOOKUP_PROFILES)
async def test_l2_complete_after_delta_and_l3_as_of_on_provider_clock(provider, pid: str) -> None:
    prof = profile(pid)
    delta = prof.capability.lookup_lag_ms
    actual = prof.behavior.actual_lookup_lag_ms
    await provider.configure(prof)
    await provider.pay("l2")
    (payment,) = await provider.ledger("l2")
    committed = ts(payment["committed_at"])
    seen = []
    deadline = committed + ms(delta + 300)
    while True:
        before = await provider.now()
        resp = await provider.lookup("l2")
        after = await provider.now()
        as_of = ts(resp.json()["as_of"])
        assert before <= as_of <= after, "L3: as_of is the provider-clock evaluation time"
        found = len(resp.json()["records"]) == 1
        seen.append((as_of, found))
        if as_of >= committed + ms(delta):
            assert found, "L2: a commit must be visible to every lookup with as_of ≥ committed_at + δ"
        if as_of < committed + ms(actual):
            assert not found, "implementation check: visibility follows the configured lag"
        if as_of > deadline:
            break
        await asyncio.sleep(0.04)
    assert any(f for _, f in seen) and any(a >= committed + ms(delta) for a, _ in seen)


@pytest.mark.parametrize("pid", LOOKUP_PROFILES)
async def test_l4_all_matching_records_including_declines(provider, pid: str) -> None:
    prof = profile(pid)
    rules = [{"client_reference": "l4", "ingress_index": 3, "fault": "FORCE_DECLINE"}]
    await provider.configure(prof, rules)
    for _ in range(3):  # no Idempotency-Key header → three separate records
        await provider.pay("l4")
    await provider.sleep_until(await provider.now() + ms(prof.capability.lookup_lag_ms + 50))
    records = (await provider.lookup("l4")).json()["records"]
    assert sorted(r["status"] for r in records) == ["declined", "succeeded", "succeeded"]


async def test_l5_lookup_scoped_to_reference(provider) -> None:
    await provider.configure(profile("P1"))
    await provider.pay("l5-a")
    await provider.pay("l5-b")
    await provider.sleep_until(await provider.now() + ms(600))
    records = (await provider.lookup("l5-a")).json()["records"]
    assert [r["client_reference"] for r in records] == ["l5-a"]


async def test_f8_lookup_unavailable_then_recovers(provider) -> None:
    rules = [{"client_reference": "l8", "kind": "lookup_ref", "ingress_index": 1, "fault": "LOOKUP_UNAVAILABLE"}]
    await provider.configure(profile("P1"), rules)
    assert (await provider.lookup("l8")).status_code == 503
    assert (await provider.lookup("l8")).status_code == 200
    assert [f["fault"] for f in await provider.faults()] == ["LOOKUP_UNAVAILABLE"]


@pytest.mark.parametrize("pid", D_PROFILES)
async def test_d_commit_after_deadline_is_rolled_back_and_key_released(provider, pid: str) -> None:
    prof = profile(pid)
    rules = [{"client_reference": "d1", "ingress_index": 1, "fault": "DELAYED_COMMIT", "params": {"delay_ms": 900}}]
    await provider.configure(prof, rules)
    deadline = await provider.now() + ms(400)  # passes the receipt check, expires before commit
    r = await provider.pay("d1", key="d1", deadline=deadline)
    assert r.status == 409 and r.code == "DEADLINE_EXCEEDED"
    assert await provider.ledger("d1", status=None) == []
    (receipt,) = await provider.receipts("d1")
    assert receipt["disposition"] == "deadline_rejected_at_commit"
    assert ts(receipt["arrived_at"]) <= deadline, "precondition: arrival was before the deadline"
    if prof.capability.idempotency:  # K6: the reservation is released with the rollback
        assert not [k for k in (await provider.keys())["keys"] if k["key"] == "d1"]
        retry = await provider.pay("d1", key="d1", deadline=await provider.now() + ms(2000))
        assert retry.status == 201 and not retry.replayed
        assert len(await provider.ledger("d1")) == 1


@pytest.mark.parametrize("pid", ["P3", "P4n", "P5", "P6"])
async def test_no_d_means_deadline_header_is_ignored(provider, pid: str) -> None:
    rules = [{"client_reference": "d2", "ingress_index": 1, "fault": "DELAYED_COMMIT", "params": {"delay_ms": 600}}]
    await provider.configure(profile(pid), rules)
    r = await provider.pay("d2", deadline=await provider.now() + ms(200))
    assert r.status == 201
    (payment,) = await provider.ledger("d2")
    assert ts(payment["committed_at"]) > ts(payment["deadline"]), "committed after its (unenforced) deadline"
