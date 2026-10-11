"""RAEG behind the driver-facing contract (API side only).

Topology (DESIGN §5.1; M7): the API process only records the submission (T1) and waits for the outcome.
All execution — claims, dispatch, recovery, late evidence, duplicate audits — runs in the separate
`raeg-worker` process (systems/raeg_worker.py). A worker crash therefore leaves the API up, which is the
same topology as Temporal (API shim + worker) and makes FC-worker crashes equivalent across the two.
"""

from __future__ import annotations

import asyncio
import os

from raeg_common.models import PaymentPayload
from raeg_common.profiles import load_all
from raeg_gateway.controller import Policy
from raeg_gateway.store import GatewayStore, create_pool, migrate
from raeg_gateway.transitions import TERMINAL, State
from systems.base import DECLINED, ESCALATED, NOT_EXECUTED, OK, PENDING, Native, SystemConfig

STATE_TO_NATIVE = {State.SUCCEEDED: OK, State.DECLINED: DECLINED, State.NOT_EXECUTED: NOT_EXECUTED,
                   State.ESCALATED: ESCALATED}
WAIT_POLL_S = 0.02


def policy_for(config: SystemConfig) -> Policy:
    return Policy(**{"attempt_ms": config.attempt_ms, "margin_ms": config.margin_ms,
                     "client_timeout_ms": config.client_timeout_ms, "max_dispatch": config.max_dispatch,
                     "backoff_base_ms": config.backoff_base_ms, "max_backoff_ms": config.max_backoff_ms,
                     "send_deadline": config.send_deadline, "system_id": config.system_id, **config.policy})


async def open_store(policy_version: str) -> GatewayStore:
    if os.environ.get("RAEG_DB_ADMIN_DSN"):
        await migrate(os.environ["RAEG_DB_ADMIN_DSN"])  # idempotent (advisory lock); owner role
    pool = await create_pool(os.environ.get("RAEG_DB_APP_DSN", "postgresql://raeg_app:raeg_app@raeg-db:5432/raeg"))
    return GatewayStore(pool, os.environ.get("CODE_SHA", "unknown"), policy_version)


class RaegSystem:
    def __init__(self, config: SystemConfig) -> None:
        self.config = config
        self.policy = policy_for(config)
        self.profiles = load_all()
        self.store: GatewayStore | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self.store is None:
                self.store = await open_store(self.policy.version)

    async def stop(self) -> None:
        return None

    async def submit(self, action_key: str, payload: PaymentPayload, profile_id: str) -> Native:
        await self.start()
        assert self.store is not None
        cap = self.profiles[profile_id].capability
        res = await self.store.submit(action_key, payload.model_dump(), payload.fingerprint(), profile_id,
                                      cap.model_dump(mode="json"), self.policy.to_json(), f"{self.config.system_id}-api")
        while True:  # the caller's wait_s bounds this (asyncio.wait_for in serve.py)
            native = await self._native(action_key, extra={"submission": res.outcome})
            if native.status != PENDING:
                return native
            await asyncio.sleep(WAIT_POLL_S)

    async def status(self, action_key: str) -> Native:
        await self.start()
        return await self._native(action_key)

    async def _native(self, action_key: str, extra: dict | None = None) -> Native:
        assert self.store is not None
        row = await self.store.snapshot(action_key)
        if row is None:
            return Native(self.config.system_id, action_key, PENDING, detail={"unknown_to_system": True})
        state = State(row["state"])
        status = STATE_TO_NATIVE.get(state, PENDING) if (state in TERMINAL or state == State.ESCALATED) else PENDING
        return Native(self.config.system_id, action_key, status, row["provider_txn_id"],
                      detail={"state": state.value, "escalation_reason": row["escalation_reason"],
                              "attempts": row["attempt_count"], "duplicate_detected": row["duplicate_detected"],
                              **(extra or {})})
