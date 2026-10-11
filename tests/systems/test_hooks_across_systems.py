"""The same crash hook (CP3: response received, not yet persisted/returned) fires at the same point in
three architectures, through the shared client: in-memory B1, Temporal B3 (worker process), RAEG.
The hook calls os._exit(137) inside a container; Docker restarts it."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from .conftest import (SYSTEMS_URL, arm_hooks, configure, disarm_hooks, exchange_log, new_key, restart_count, submit,
                       wait_healthy, wait_restarted)


@pytest.fixture(autouse=True)
def _disarm():
    disarm_hooks()
    yield
    disarm_hooks()


async def crash_hooks_fired(system_id: str, key: str, worker: bool = False) -> list[dict]:
    return [e for e in exchange_log(system_id, worker) if e.get("action_key") == key and e.get("event") == "hook_fired"]


async def test_cp3_b1_in_memory_state_is_lost(systems, provider) -> None:
    key = new_key("B1")
    await configure(provider, "P1", key)
    before = restart_count("systems")
    arm_hooks([{"action_key": key, "attempt_no": 1, "point": "CP3"}])
    with pytest.raises(httpx.TransportError):  # the process died while the driver was waiting
        await submit(systems, "B1", key, "P1")
    await wait_restarted("systems", before, f"{SYSTEMS_URL}/healthz")
    disarm_hooks()
    assert [e["point"] for e in await crash_hooks_fired("B1", key)] == ["CP3"]
    assert len(await provider.ledger(key)) == 1, "the payment committed before the crash"
    status = (await systems.get(f"/s/B1/v1/actions/{key}")).json()
    assert status["status"] == "pending" and status["detail"].get("unknown_to_system"), "in-memory state lost"


async def test_cp3_b3_worker_crash_is_retried_by_temporal(systems, provider) -> None:
    key = new_key("B3")
    await configure(provider, "P1", key)
    arm_hooks([{"action_key": key, "attempt_no": 1, "point": "CP3"}])
    before = restart_count("temporal-worker")
    native = await submit(systems, "B3", key, "P1", wait_s=80)
    assert restart_count("temporal-worker") > before, "the worker process really died and was restarted"
    assert [e["point"] for e in await crash_hooks_fired("B3", key, worker=True)] == ["CP3"]
    receipts = await provider.receipts(key)
    assert native["status"] == "ok" and [r["attempt_hdr"] for r in receipts] == ["1", "2"]
    assert receipts[1]["disposition"] == "replayed", "attempt 2 reused the key (P1 has I)"
    assert len(await provider.ledger(key)) == 1
    await wait_healthy("temporal-worker")


async def test_cp3_raeg_recovers_after_restart_without_traffic(systems, provider) -> None:
    key = new_key("RAEG-strict")
    await configure(provider, "P1", key)
    before = restart_count("systems")
    arm_hooks([{"action_key": key, "attempt_no": 1, "point": "CP3"}])
    with pytest.raises(httpx.TransportError):
        await submit(systems, "RAEG-strict", key, "P1")
    await wait_restarted("systems", before, f"{SYSTEMS_URL}/healthz")
    disarm_hooks()
    assert [e["point"] for e in await crash_hooks_fired("RAEG-strict", key)] == ["CP3"]
    for _ in range(120):  # the background loop (started at boot) sweeps the lease and verifies
        status = (await systems.get(f"/s/RAEG-strict/v1/actions/{key}")).json()
        if status["status"] != "pending":
            break
        await asyncio.sleep(0.25)
    assert status["status"] == "ok" and status["detail"]["attempts"] == 1, status
    assert len(await provider.ledger(key)) == 1 and len(await provider.receipts(key)) == 1
