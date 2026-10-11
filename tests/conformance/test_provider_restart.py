"""K7 with a real provider process crash (SIGKILL) and restart — not the admin shortcut.

A request is held inside the provider after its idempotency reservation committed (IN_PROGRESS)
and before its payment transaction began. The container is SIGKILLed, then started again. On
startup the provider must release the stuck reservation (K1 guarantees it did not commit), keep
completed records, and a retry with the same key must execute exactly once.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import profile

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.restart


def compose(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DOCKER_CONTEXT": os.environ.get("DOCKER_CONTEXT", "colima"), "PROVIDER_ALLOW_RESET": "1"}
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=180, check=True)


async def test_k7_sigkill_with_in_progress_reservation(provider) -> None:
    rules = [{"client_reference": "r1", "ingress_index": 1, "fault": "DELAYED_COMMIT", "params": {"delay_ms": 60_000}}]
    await provider.configure(profile("P1"), rules)
    done = await provider.pay("r0", key="r0")
    assert done.status == 201

    in_flight = asyncio.create_task(provider.pay("r1", key="r1", timeout=90))
    for _ in range(200):
        keys = {k["key"]: k["state"] for k in (await provider.keys())["keys"]}
        if keys.get("r1") == "IN_PROGRESS":
            break
        await asyncio.sleep(0.05)
    assert keys.get("r1") == "IN_PROGRESS", "precondition: reservation committed before the crash"

    compose("kill", "-s", "SIGKILL", "provider")
    crashed = await in_flight
    assert crashed.status is None and crashed.error is not None, "the client saw the crash as a transport failure"
    compose("up", "-d", "--wait", "provider")
    # `--wait` is container-side health; the host port forward can lag behind it (N56).
    subprocess.run([sys.executable, str(ROOT / "scripts" / "wait_ready.py"), "http://127.0.0.1:18001/healthz",
                    "http://127.0.0.1:19001/healthz"], check=True, timeout=300)

    ready = [line for line in compose("logs", "--no-color", "provider").stdout.splitlines() if "provider ready" in line]
    assert ready and ready[-1].endswith("released_on_recovery=1"), ready[-1:]

    state = await provider.keys()
    assert "r1" not in {k["key"] for k in state["keys"]}, "stuck reservation released on startup"
    assert {k["key"]: k["state"] for k in state["keys"]}.get("r0") == "COMPLETED", "completed record survived"
    assert [e["event"] for e in state["events"] if e["key"] == "r1"][-1] == "released_on_recovery"
    assert await provider.ledger("r1", status=None) == [], "nothing was committed for the crashed request"
    (receipt,) = await provider.receipts("r1")
    assert receipt["disposition"] is None, "the crashed request has no outcome row: evidence of the crash"

    # After restart the provider runs its startup profile (P1) with an empty fault plan.
    assert (await provider.pay("r0", key="r0")).replayed
    retry = await provider.pay("r1", key="r1")
    assert retry.status == 201 and not retry.replayed
    assert len(await provider.ledger("r1")) == 1
