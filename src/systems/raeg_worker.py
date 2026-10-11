"""RAEG recovery worker process (DESIGN §5.1): claims, dispatch, sweeper, controller, late evidence and
duplicate audits for every configured RAEG system, each scoped to its own actions (N32).

Crash hooks in the shared client fire here, so an FC-worker crash kills only this process; Docker
restarts it and the sweeper resumes from the durable store.
"""

from __future__ import annotations

import asyncio
import os
import uuid

from systems.base import load_systems, make_client
from systems.raeg_system import open_store, policy_for
from raeg_gateway.engine import RecoveryEngine

POLL_S = 0.02


async def run_system(config) -> None:
    policy = policy_for(config)
    store = await open_store(policy.version)
    engine = RecoveryEngine(store, make_client(config, worker_tag="-worker"),
                            f"{config.system_id}-worker-{uuid.uuid4().hex[:6]}", system_id=config.system_id)
    print(f"raeg worker ready for {config.system_id}", flush=True)
    while True:
        try:
            progressed = await engine.run_once(limit=100)
        except Exception as exc:  # keep recovering; visible in container logs
            print(f"[{config.system_id}] worker pass error: {exc!r}", flush=True)
            progressed = 0
        if not progressed:
            await asyncio.sleep(POLL_S)


async def main() -> None:
    enabled = os.environ.get("SYSTEMS", "").split(",") if os.environ.get("SYSTEMS") else None
    configs = [c for sid, c in load_systems().items() if c.kind == "raeg" and (enabled is None or sid in enabled)]
    if not configs:
        print("no RAEG system enabled; idle", flush=True)
        await asyncio.Event().wait()
    await asyncio.gather(*(run_system(c) for c in configs))


if __name__ == "__main__":
    asyncio.run(main())
