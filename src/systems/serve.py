"""One HTTP service hosting the systems under test behind the driver-facing contract (DESIGN §5.4):

  POST /s/{system_id}/v1/actions            {action_key, payload, profile_id, wait_s?} -> native outcome
  GET  /s/{system_id}/v1/actions/{key}      -> native outcome (status "pending" while unresolved)

Experiments exercise one system at a time; hosting them in one process only shares the container.
"""

from __future__ import annotations

import asyncio
import os
import time

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from raeg_common.models import PaymentPayload
from systems.base import PENDING, Native, SystemUnderTest, load_systems
from systems.raeg_system import RaegSystem
from systems.retry_baselines import RetryBaseline
from systems.temporal_wf.system import TemporalSystem

app = FastAPI(title="systems-under-test", docs_url=None, redoc_url=None, openapi_url=None)
SYSTEMS: dict[str, SystemUnderTest] = {}


def build() -> None:
    enabled = os.environ.get("SYSTEMS", "").split(",") if os.environ.get("SYSTEMS") else None
    for sid, cfg in load_systems().items():
        if enabled and sid not in enabled:
            continue
        SYSTEMS[sid] = {"retry": RetryBaseline, "temporal": TemporalSystem, "raeg": RaegSystem}[cfg.kind](cfg)


class Submit(BaseModel):
    action_key: str
    payload: PaymentPayload
    profile_id: str
    wait_s: float = 60.0


def get(system_id: str) -> SystemUnderTest:
    if system_id not in SYSTEMS:
        raise HTTPException(404, f"unknown system {system_id}")
    return SYSTEMS[system_id]


@app.post("/admin/crash")
async def admin_crash(x_crash_token: str = Header(default="")) -> dict:
    """FC-api: fail-stop crash of this API process (os._exit after replying), triggered by the harness."""
    token = os.environ.get("CRASH_TOKEN", "")
    if not token or x_crash_token != token:
        raise HTTPException(403, "crash token required")
    asyncio.get_running_loop().call_later(0.05, os._exit, 137)
    return {"crashing": True}


@app.get("/admin/ready/{system_id}")
async def admin_ready(system_id: str) -> dict:
    """N60: end-to-end readiness. Temporal systems run one probe workflow (workflow + activity task) through
    the same client and task queue; other systems are ready when this process answers."""
    system = get(system_id)
    if not isinstance(system, TemporalSystem):
        return {"ready": True, "probe": None}
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(system.probe(), 10)
    except Exception as exc:  # not ready yet: report, the harness retries
        raise HTTPException(503, f"probe failed: {type(exc).__name__}: {exc}") from exc
    return {"ready": True, "probe": "temporal_workflow", "probe_ms": round((time.monotonic() - t0) * 1000, 1)}


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "systems": sorted(SYSTEMS)}


@app.post("/s/{system_id}/v1/actions")
async def submit(system_id: str, body: Submit) -> dict:
    system = get(system_id)
    try:
        native = await asyncio.wait_for(system.submit(body.action_key, body.payload, body.profile_id), body.wait_s)
    except TimeoutError:
        native = Native(system_id, body.action_key, PENDING, detail={"submit_wait_expired_s": body.wait_s})
    return native.to_json()


@app.get("/s/{system_id}/v1/actions/{action_key}")
async def status(system_id: str, action_key: str) -> dict:
    return (await get(system_id).status(action_key)).to_json()


def main() -> None:
    build()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), http="h11", loop="asyncio",
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
