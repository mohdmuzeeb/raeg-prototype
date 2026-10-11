"""Idempotency contract K1–K7 (DESIGN §2.4)."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from .conftest import body, profile

COMBOS = [(pid, overlap) for pid in ("P1", "P2") for overlap in ("reject", "serialize")]


def rule(ref: str, idx: int, fault: str, **params) -> dict:
    return {"client_reference": ref, "ingress_index": idx, "fault": fault, "params": params}


@pytest.mark.parametrize(("pid", "overlap"), COMBOS)
async def test_k2_overlapping_request_has_no_effect(provider, pid: str, overlap: str) -> None:
    await provider.configure(profile(pid, behavior={"overlap": overlap}), [rule("k2", 1, "DELAYED_COMMIT", delay_ms=800)])
    first = asyncio.create_task(provider.pay("k2", key="k2"))
    await asyncio.sleep(0.2)
    second = await provider.pay("k2", key="k2")
    first_r = await first
    assert first_r.status == 201
    if overlap == "reject":
        assert second.status == 409 and second.code == "IDEMPOTENCY_IN_PROGRESS"
    else:
        assert second.status == 201 and second.replayed
        assert second.json["provider_txn_id"] == first_r.json["provider_txn_id"]
    assert len(await provider.ledger("k2")) == 1


@pytest.mark.parametrize(("pid", "overlap"), COMBOS)
async def test_k1_concurrent_first_requests_commit_once(provider, pid: str, overlap: str) -> None:
    await provider.configure(profile(pid, behavior={"overlap": overlap}))
    results = await asyncio.gather(*(provider.pay("k1", key="k1") for _ in range(12)))
    assert len(await provider.ledger("k1")) == 1
    statuses = sorted(r.status for r in results)
    assert statuses.count(201) >= 1
    assert all(s in (201, 409) for s in statuses)
    if overlap == "serialize":
        assert all(r.status == 201 for r in results), "serialize mode replays to every waiter"
    txns = {r.json["provider_txn_id"] for r in results if r.status == 201}
    assert len(txns) == 1


async def test_k3_decline_is_replayed_not_reexecuted(provider) -> None:
    await provider.configure(profile("P1"), [rule("k3", 1, "FORCE_DECLINE")])
    first = await provider.pay("k3", key="k3")
    second = await provider.pay("k3", key="k3")
    assert first.status == 402 and first.json["status"] == "declined"
    assert second.status == 402 and second.replayed
    assert await provider.ledger("k3") == []
    assert len(await provider.ledger("k3", status="declined")) == 1


async def test_k4_mismatched_body_is_rejected_without_effect(provider) -> None:
    await provider.configure(profile("P1"))
    assert (await provider.pay("k4", key="k4", amount=100)).status == 201
    r = await provider.pay("k4", key="k4", amount=999)
    assert r.status == 422 and r.code == "IDEMPOTENCY_MISMATCH"
    assert len(await provider.ledger("k4", status=None)) == 1


@pytest.mark.parametrize("fault", [("ERROR_5XX_PRE", {}), ("PRE_COMMIT_STALL", {"stall_ms": 300})])
async def test_k6_pre_commit_abort_releases_reservation(provider, fault) -> None:
    name, params = fault
    await provider.configure(profile("P1"), [rule("k6", 1, name, **params)])
    first = await provider.pay("k6", key="k6")
    assert first.status == 503
    assert not [k for k in (await provider.keys())["keys"] if k["key"] == "k6"]
    assert "released" in [e["event"] for e in (await provider.keys())["events"] if e["key"] == "k6"]
    second = await provider.pay("k6", key="k6")
    assert second.status == 201 and not second.replayed
    assert len(await provider.ledger("k6")) == 1


async def test_k7_stuck_reservation_released_on_recovery(provider, provider_db) -> None:
    await provider.configure(profile("P1"))
    await provider.pay("k7-done", key="k7-done")
    sha = hashlib.sha256(json.dumps(body("k7"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    await provider_db.execute(
        """INSERT INTO idempotency_keys (key, body_sha256, state, created_at, owner_receipt_id)
           VALUES ('k7', $1, 'IN_PROGRESS', provider_now(), 999999)""",
        sha,
    )
    stuck = await provider.pay("k7", key="k7")
    assert stuck.status == 409 and stuck.code == "IDEMPOTENCY_IN_PROGRESS"
    released = (await provider.admin.post("/admin/recover")).json()["released"]
    assert released == 1
    keys = {k["key"]: k["state"] for k in (await provider.keys())["keys"]}
    assert "k7" not in keys and keys["k7-done"] == "COMPLETED", "completed records are untouched"
    assert (await provider.pay("k7", key="k7")).status == 201
    assert len(await provider.ledger("k7")) == 1


async def test_no_key_header_means_no_deduplication_even_with_I(provider) -> None:
    await provider.configure(profile("P1"))
    await provider.pay("k8")
    await provider.pay("k8")
    assert len(await provider.ledger("k8")) == 2
