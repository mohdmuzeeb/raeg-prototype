"""RAEG behind the driver-facing contract: submit, drive to quiescence, and keep a background recovery
loop (sweeper, controller, late evidence, duplicate audits) running for every action."""

from __future__ import annotations

import asyncio
import os
import uuid

from raeg_common.models import PaymentPayload
from raeg_common.profiles import load_all
from raeg_gateway.controller import Policy
from raeg_gateway.engine import RecoveryEngine
from raeg_gateway.store import GatewayStore, create_pool, migrate
from raeg_gateway.transitions import TERMINAL, State
from systems.base import DECLINED, ESCALATED, NOT_EXECUTED, OK, PENDING, Native, SystemConfig, make_client

STATE_TO_NATIVE = {State.SUCCEEDED: OK, State.DECLINED: DECLINED, State.NOT_EXECUTED: NOT_EXECUTED,
                   State.ESCALATED: ESCALATED}


def policy_for(config: SystemConfig) -> Policy:
    return Policy(**{"attempt_ms": config.attempt_ms, "margin_ms": config.margin_ms,
                     "client_timeout_ms": config.client_timeout_ms, "max_dispatch": config.max_dispatch,
                     "backoff_base_ms": config.backoff_base_ms, "max_backoff_ms": config.max_backoff_ms,
                     "send_deadline": config.send_deadline, "system_id": config.system_id, **config.policy})


class RaegSystem:
    def __init__(self, config: SystemConfig) -> None:
        self.config = config
        self.policy = policy_for(config)
        self.profiles = load_all()
        self.engine: RecoveryEngine | None = None
        self._bg: asyncio.Task | None = None

    async def start(self) -> None:
        if self.engine is not None:
            return
        if os.environ.get("RAEG_DB_ADMIN_DSN"):
            await migrate(os.environ["RAEG_DB_ADMIN_DSN"])  # idempotent; runs as the owner role
        pool = await create_pool(os.environ.get("RAEG_DB_APP_DSN", "postgresql://raeg_app:raeg_app@raeg-db:5432/raeg"))
        store = GatewayStore(pool, os.environ.get("CODE_SHA", "unknown"), self.policy.version)
        self.engine = RecoveryEngine(store, make_client(self.config), f"{self.config.system_id}-{uuid.uuid4().hex[:6]}",
                                     system_id=self.config.system_id)
        self._bg = asyncio.create_task(self._background())

    async def _background(self) -> None:
        assert self.engine is not None
        while True:
            try:
                await self.engine.run_once()
            except Exception as exc:  # keep recovering; the error is visible in container logs
                print(f"[{self.config.system_id}] background pass error: {exc!r}", flush=True)
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        if self._bg:
            self._bg.cancel()

    async def submit(self, action_key: str, payload: PaymentPayload, profile_id: str) -> Native:
        await self.start()
        assert self.engine is not None
        outcome = await self.engine.submit(action_key, payload, profile_id, self.profiles[profile_id].capability, self.policy)
        state = await self.engine.run_to_quiescence(action_key, max_steps=10_000, poll_s=0.05)
        return await self._native(action_key, extra={"submission": outcome, "state": state.value})

    async def status(self, action_key: str) -> Native:
        await self.start()
        return await self._native(action_key)

    async def _native(self, action_key: str, extra: dict | None = None) -> Native:
        assert self.engine is not None
        row = await self.engine.store.snapshot(action_key)
        if row is None:
            return Native(self.config.system_id, action_key, PENDING, detail={"unknown_to_system": True})
        state = State(row["state"])
        status = STATE_TO_NATIVE.get(state, PENDING) if (state in TERMINAL or state == State.ESCALATED) else PENDING
        return Native(self.config.system_id, action_key, status, row["provider_txn_id"],
                      detail={"state": state.value, "escalation_reason": row["escalation_reason"],
                              "attempts": row["attempt_count"], "duplicate_detected": row["duplicate_detected"],
                              **(extra or {})})
