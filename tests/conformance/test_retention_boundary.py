"""KB1–KB6: delayed originals and retries overlapping near the key-retention boundary (DESIGN §2.4 d).

Each test first asserts its *timing precondition* from the provider's own receipt log (arrival
times on the provider clock). If scheduling jitter put a request on the wrong side of the boundary,
the test fails on the precondition rather than passing for the wrong reason.

Run on the short-τ profiles P2 and P7 with their declared τ, for both overlap modes.
"""

from __future__ import annotations

import asyncio

import pytest

from .conftest import ms, profile, ts

DELTA_MS = 400  # margin from the boundary; well above observed request jitter (tens of ms)
COMBOS = [(pid, overlap) for pid in ("P2", "P7") for overlap in ("reject", "serialize")]


def hold(ref: str, idx: int, hold_ms: int) -> dict:
    return {"client_reference": ref, "ingress_index": idx, "fault": "INGRESS_HOLD", "params": {"hold_ms": hold_ms}}


def tau_ms(pid: str) -> int:
    value = profile(pid).capability.key_ttl_ms
    assert value is not None
    return value


async def arrivals(provider, ref: str) -> list:
    return [ts(r["arrived_at"]) for r in await provider.receipts(ref)]


@pytest.mark.parametrize(("pid", "overlap"), COMBOS)
async def test_kb1_replay_just_before_expiry(provider, pid: str, overlap: str) -> None:
    tau = tau_ms(pid)
    await provider.configure(profile(pid, behavior={"overlap": overlap}))
    assert (await provider.pay("b1", key="b1")).status == 201
    created = await provider.key_created_at("b1")
    await provider.sleep_until(created + ms(tau - DELTA_MS))
    r = await provider.pay("b1", key="b1")
    arrival = (await arrivals(provider, "b1"))[1]
    assert arrival < created + ms(tau), "precondition: replay must arrive before created_at + τ"
    assert r.status == 201 and r.replayed
    assert len(await provider.ledger("b1")) == 1


@pytest.mark.parametrize(("pid", "overlap"), COMBOS)
async def test_kb2_after_expiry_is_processed_as_new(provider, pid: str, overlap: str) -> None:
    tau = tau_ms(pid)
    await provider.configure(profile(pid, behavior={"overlap": overlap}))
    assert (await provider.pay("b2", key="b2")).status == 201
    created = await provider.key_created_at("b2")
    await provider.sleep_until(created + ms(tau + DELTA_MS))
    r = await provider.pay("b2", key="b2")
    arrival = (await arrivals(provider, "b2"))[1]
    assert arrival >= created + ms(tau), "precondition: request must arrive at/after created_at + τ"
    assert r.status == 201 and not r.replayed
    assert len(await provider.ledger("b2")) == 2, "K5 permits a new effect after retention: the hazard"
    assert "expired" in [e["event"] for e in (await provider.keys())["events"] if e["key"] == "b2"]


@pytest.mark.parametrize(("pid", "overlap"), COMBOS)
async def test_kb3_delayed_original_overtaken_within_retention(provider, pid: str, overlap: str) -> None:
    tau = tau_ms(pid)
    await provider.configure(profile(pid, behavior={"overlap": overlap}), [hold("b3", 1, 1000)])
    original = asyncio.create_task(provider.pay("b3", key="b3"))  # ingress #1, held before receipt
    await asyncio.sleep(0.15)
    retry = await provider.pay("b3", key="b3")  # ingress #2, arrives first and creates the record
    orig = await original
    created = await provider.key_created_at("b3")
    receipts = await provider.receipts("b3")
    assert receipts[0]["ingress_index"] == 2 and receipts[1]["ingress_index"] == 1, "precondition: retry arrived first"
    assert ts(receipts[1]["arrived_at"]) < created + ms(tau), "precondition: original arrived within retention"
    assert retry.status == 201 and not retry.replayed
    assert orig.status == 201 and orig.replayed
    assert len(await provider.ledger("b3")) == 1


@pytest.mark.parametrize(("pid", "overlap"), COMBOS)
async def test_kb4_delayed_original_after_retention_creates_second_effect(provider, pid: str, overlap: str) -> None:
    tau = tau_ms(pid)
    await provider.configure(profile(pid, behavior={"overlap": overlap}), [hold("b4", 1, tau + 1000)])
    original = asyncio.create_task(provider.pay("b4", key="b4"))  # no deadline header
    await asyncio.sleep(0.15)
    retry = await provider.pay("b4", key="b4")
    orig = await original
    created_retry = ts([e for e in (await provider.keys())["events"] if e["key"] == "b4" and e["event"] == "reserved"][0]["at"])
    receipts = await provider.receipts("b4")
    assert receipts[1]["ingress_index"] == 1
    assert ts(receipts[1]["arrived_at"]) >= created_retry + ms(tau), "precondition: original arrived after expiry"
    assert retry.status == 201 and orig.status == 201 and not orig.replayed
    assert len(await provider.ledger("b4")) == 2, "the hazard the §2.6 deadline-based TTL guard must exclude"


@pytest.mark.parametrize(("pid", "overlap"), COMBOS)
async def test_kb5_deadline_bounds_the_delayed_original(provider, pid: str, overlap: str) -> None:
    tau = tau_ms(pid)
    assert profile(pid).capability.deadline_enforced
    await provider.configure(profile(pid, behavior={"overlap": overlap}), [hold("b5", 1, tau + 1000)])
    deadline = await provider.now() + ms(1500)
    original = asyncio.create_task(provider.pay("b5", key="b5", deadline=deadline))
    await asyncio.sleep(0.15)
    retry = await provider.pay("b5", key="b5", deadline=await provider.now() + ms(1500))
    orig = await original
    created = await provider.key_created_at("b5")
    receipts = await provider.receipts("b5")
    assert deadline < created + ms(tau), "precondition: original's deadline precedes the retry record's expiry"
    assert ts(receipts[1]["arrived_at"]) > deadline, "precondition: original arrived after its deadline"
    assert retry.status == 201
    assert orig.status == 409 and orig.code == "DEADLINE_EXCEEDED"
    assert receipts[1]["disposition"] == "deadline_rejected_at_receipt"
    assert len(await provider.ledger("b5")) == 1


@pytest.mark.parametrize(("pid", "overlap"), COMBOS)
async def test_kb6_in_progress_record_never_expires(provider, pid: str, overlap: str) -> None:
    tau = tau_ms(pid)
    rules = [{"client_reference": "b6", "ingress_index": 1, "fault": "DELAYED_COMMIT", "params": {"delay_ms": tau + 1500}}]
    await provider.configure(profile(pid, behavior={"overlap": overlap}), rules)
    first = asyncio.create_task(provider.pay("b6", key="b6"))
    for _ in range(100):
        if [k for k in (await provider.keys())["keys"] if k["key"] == "b6"]:
            break
        await asyncio.sleep(0.02)
    created = await provider.key_created_at("b6")
    await provider.sleep_until(created + ms(tau + DELTA_MS))
    second = await provider.pay("b6", key="b6")
    first_r = await first
    (payment,) = await provider.ledger("b6")
    second_arrival = (await arrivals(provider, "b6"))[1]
    assert second_arrival >= created + ms(tau), "precondition: overlap occurs after created_at + τ"
    assert ts(payment["committed_at"]) > second_arrival, "precondition: first was still IN_PROGRESS at that arrival"
    assert first_r.status == 201
    if overlap == "reject":
        assert second.status == 409 and second.code == "IDEMPOTENCY_IN_PROGRESS"
    else:
        assert second.status == 201 and second.replayed
    assert len(await provider.ledger("b6")) == 1
