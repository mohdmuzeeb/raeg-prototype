"""Driver-facing side of B3/B3L: start (or attach to) the workflow with id = k and await its result."""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from raeg_common.models import PaymentPayload
from raeg_common.profiles import load_all
from systems.base import PENDING, Native, SystemConfig
from systems.temporal_wf.worker import TASK_QUEUE
from systems.temporal_wf.workflows import PaymentWorkflow, ReadinessProbeWorkflow


class TemporalSystem:
    def __init__(self, config: SystemConfig) -> None:
        self.config = config
        self.client: Client | None = None
        self.profiles = load_all()

    async def start(self) -> None:
        if self.client is None:
            self.client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "temporal:7233"), namespace="default")

    async def stop(self) -> None:
        return None

    async def probe(self) -> dict:
        """N60: one end-to-end readiness probe through this system's client and task queue."""
        await self.start()
        assert self.client is not None
        return await self.client.execute_workflow(ReadinessProbeWorkflow.run, id=f"readiness-probe-{uuid.uuid4()}",
                                                  task_queue=TASK_QUEUE, execution_timeout=timedelta(seconds=10))

    def _input(self, action_key: str, payload: PaymentPayload, profile_id: str) -> dict:
        c = self.config
        return {"variant": c.variant, "system_id": c.system_id, "action_key": action_key, "payload": payload.model_dump(),
                "attempt_ms": c.attempt_ms, "margin_ms": c.margin_ms, "client_timeout_ms": c.client_timeout_ms,
                "max_dispatch": c.max_dispatch,
                "backoff_base_ms": c.backoff_base_ms, "max_backoff_ms": c.max_backoff_ms,
                # documented lookup lag of the provider profile (all B3L knows about the provider)
                "lookup_lag_ms": self.profiles[profile_id].capability.lookup_lag_ms}

    async def submit(self, action_key: str, payload: PaymentPayload, profile_id: str) -> Native:
        await self.start()
        assert self.client is not None
        try:
            handle = await self.client.start_workflow(
                PaymentWorkflow.run, self._input(action_key, payload, profile_id), id=action_key, task_queue=TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
        except WorkflowAlreadyStartedError:  # completed earlier: attach to the existing result
            handle = self.client.get_workflow_handle(action_key)
        return self._native(action_key, await handle.result())

    async def status(self, action_key: str) -> Native:
        await self.start()
        assert self.client is not None
        handle = self.client.get_workflow_handle(action_key)
        desc = await handle.describe()
        if desc.status == WorkflowExecutionStatus.RUNNING:
            return Native(self.config.system_id, action_key, PENDING, detail={"workflow": "running"})
        return self._native(action_key, await handle.result())

    def _native(self, action_key: str, res: dict) -> Native:
        return Native(self.config.system_id, action_key, res["status"], res.get("provider_txn_id"),
                      res.get("error_class"), {**res.get("detail", {}), "workflow": "completed"})
