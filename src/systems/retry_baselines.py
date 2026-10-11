"""B1 (naive retry) and B2 (idempotency-only). In-memory state only: a crash loses everything, and the
agent's resubmission starts over (DESIGN §5.4, §5.5)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from raeg_common.models import Observation, PaymentPayload
from systems.base import DECLINED, ERROR, OK, PENDING, Native, SystemConfig, make_client


class RetryBaseline:
    def __init__(self, config: SystemConfig, provider_url: str | None = None) -> None:
        self.config = config
        self.client = make_client(config, provider_url=provider_url)
        self.outcomes: dict[str, Native] = {}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self.client.aclose()

    async def submit(self, action_key: str, payload: PaymentPayload, profile_id: str) -> Native:
        """Every submission executes (no local de-duplication): that is the baseline's definition."""
        c = self.config
        self.outcomes[action_key] = Native(c.system_id, action_key, PENDING)
        last = None
        for n in range(1, c.max_dispatch + 1):
            start = datetime.now(UTC)
            deadline = start + timedelta(milliseconds=c.attempt_ms)
            r = await self.client.dispatch(
                action_key, payload, n, deadline, action_key if c.idempotency_key else None,
                timeout_s=(c.attempt_ms - c.margin_ms) / 1000,
            )
            last = r
            if r.observation == Observation.COMMITTED:
                out = Native(c.system_id, action_key, OK, r.provider_txn_id, detail={"attempts": n, "replayed": r.replayed})
                break
            if r.observation == Observation.DECLINED:
                out = Native(c.system_id, action_key, DECLINED, detail={"attempts": n, "http_status": r.http_status})
                break
            if r.observation == Observation.INTERNAL:
                out = Native(c.system_id, action_key, ERROR, error_class="INTERNAL", detail={"attempts": n})
                break
            if n < c.max_dispatch:
                await asyncio.sleep(c.backoff_ms(n) / 1000)
        else:
            assert last is not None
            out = Native(c.system_id, action_key, ERROR,
                         error_class=last.error_class or last.observation.value,
                         detail={"attempts": c.max_dispatch, "http_status": last.http_status})
        self.outcomes[action_key] = out
        return out

    async def status(self, action_key: str) -> Native:
        return self.outcomes.get(action_key) or Native(self.config.system_id, action_key, PENDING,
                                                       detail={"unknown_to_system": True})
