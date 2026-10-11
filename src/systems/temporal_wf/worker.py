"""Temporal worker for all B3/B3L variants (one task queue; the variant is part of the workflow input)."""

from __future__ import annotations

import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from systems.temporal_wf.activities import dispatch_payment, lookup_payment
from systems.temporal_wf.workflows import PaymentWorkflow

TASK_QUEUE = "raeg-baselines"


async def main() -> None:
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "temporal:7233"), namespace="default")
    worker = Worker(client, task_queue=TASK_QUEUE, workflows=[PaymentWorkflow],
                    activities=[dispatch_payment, lookup_payment])
    print("temporal worker ready", flush=True)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
