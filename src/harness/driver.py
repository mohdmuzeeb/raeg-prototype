"""The agent / workload driver (DESIGN §5.5), identical for every system.

Agent model:
  * open-loop arrivals at the workload's offsets; each intent is submitted `copies` times concurrently
    (copies > 1 = F5 duplicate submissions); copy 0 is the agent's primary view used for the claim;
  * on a transport failure (e.g. the system crashed) the agent resubmits the SAME (k, p) with exponential
    backoff (restart_backoff_ms, doubling, capped at max_backoff_ms) until its horizon (M7 decision:
    identical policy and horizon for every system); `max_resubmits` is only a safety cap;
  * if the system answers "pending" it polls; if the system no longer knows the action (in-memory state
    lost in a crash) the agent resubmits;
  * an action with no terminal answer by arrival + horizon_s is UNRESOLVED(horizon);
  * `accepted` records whether the system ever answered any submission/poll for that copy (HTTP 200),
    so outcomes can be reported over all intents and over intents the system actually accepted.
Every HTTP exchange is appended to driver_exchanges.jsonl; one claim line per (action, copy) to claims.jsonl.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from analysis.claims import MAPPING_VERSION, normalize
from harness.workload import Intent

TERMINAL = {"ok", "declined", "error", "not_found_after_retries", "escalated", "not_executed"}


class DriverConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_timeout_s: float = Field(default=30.0, gt=0)  # wait_s passed with each submission
    restart_backoff_ms: int = Field(default=1000, ge=0)  # first resubmit delay
    max_backoff_ms: int = Field(default=8000, ge=0)  # backoff cap (doubling from restart_backoff_ms)
    max_resubmits: int = Field(default=50, ge=0)  # safety cap only; the horizon is the real bound
    poll_ms: int = Field(default=250, gt=0)
    horizon_s: float = Field(default=60.0, gt=0)  # ≥ W_max (DESIGN §2.10a)
    drain_s: float = Field(default=10.0, ge=0)


@dataclass
class _Log:
    path: Path

    def __post_init__(self) -> None:
        self.fh = open(self.path, "a", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        self.fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        self.fh.flush()

    def close(self) -> None:
        self.fh.close()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class Driver:
    def __init__(self, base_url: str, system_id: str, profile_id: str, cfg: DriverConfig, out_dir: Path,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base = f"/s/{system_id}/v1/actions"
        self.system_id = system_id
        self.profile_id = profile_id
        self.cfg = cfg
        self.http = httpx.AsyncClient(base_url=base_url, timeout=cfg.agent_timeout_s + 10, transport=transport)
        self.exchanges = _Log(out_dir / "driver_exchanges.jsonl")
        self.claims = _Log(out_dir / "claims.jsonl")
        self.t0 = 0.0

    async def run(self, intents: list[Intent]) -> list[dict]:
        self.t0 = time.monotonic()
        tasks = [asyncio.create_task(self._intent(i)) for i in intents]
        results = [r for batch in await asyncio.gather(*tasks) for r in batch]
        await self.http.aclose()
        self.exchanges.close()
        self.claims.close()
        return results

    def _rel_ms(self) -> float:
        return (time.monotonic() - self.t0) * 1000

    async def _intent(self, intent: Intent) -> list[dict]:
        delay = intent.arrival_offset_ms / 1000 - (time.monotonic() - self.t0)
        if delay > 0:
            await asyncio.sleep(delay)
        return list(await asyncio.gather(*(self._copy(intent, c) for c in range(intent.copies))))

    async def _exchange(self, intent: Intent, copy: int, method: str, url: str, body: dict | None) -> tuple[dict | None, str | None]:
        rec: dict[str, Any] = {"action_key": intent.action_key, "copy": copy, "method": method, "url": url,
                               "request": body, "sent_at": _now(), "sent_rel_ms": self._rel_ms()}
        try:
            r = await (self.http.post(url, json=body) if method == "POST" else self.http.get(url))
            try:
                payload = r.json()
            except ValueError:
                payload = {"_raw_text": r.text}
            rec.update(status=r.status_code, response=payload, received_rel_ms=self._rel_ms())
            self.exchanges.write(rec)
            return (payload if r.status_code == 200 else None), (None if r.status_code == 200 else f"HTTP_{r.status_code}")
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            rec.update(error=type(exc).__name__, error_text=str(exc), received_rel_ms=self._rel_ms())
            self.exchanges.write(rec)
            return None, type(exc).__name__

    async def _copy(self, intent: Intent, copy: int) -> dict:
        start_rel = self._rel_ms()
        horizon_at = time.monotonic() + self.cfg.horizon_s
        body = {"action_key": intent.action_key, "payload": intent.payload, "profile_id": self.profile_id,
                "wait_s": self.cfg.agent_timeout_s}
        posts, native, last_error = 0, None, None  # resubmits = posts − 1 (only submissions actually sent)
        failures = 0  # consecutive transport failures, drives the backoff
        accepted = False
        need_submit = True
        while time.monotonic() < horizon_at:
            if need_submit:
                if posts > self.cfg.max_resubmits:  # safety cap; the horizon is the real bound
                    break
                posts += 1
                native, last_error = await self._exchange(intent, copy, "POST", self.base, body)
                need_submit = False
                if native is None:  # transport failure / crash: back off, then resubmit the same intent
                    failures += 1
                    delay = min(self.cfg.restart_backoff_ms * 2 ** (failures - 1), self.cfg.max_backoff_ms) / 1000
                    await asyncio.sleep(min(delay, max(horizon_at - time.monotonic(), 0)))
                    need_submit = True
                    continue
                failures = 0
                accepted = True
            if native and native.get("status") in TERMINAL:
                break
            await asyncio.sleep(self.cfg.poll_ms / 1000)
            polled, err = await self._exchange(intent, copy, "GET", f"{self.base}/{intent.action_key}", None)
            if polled is None:
                last_error = err
                continue  # system unreachable: keep waiting (it may be restarting)
            native = polled
            accepted = True
            if native.get("status") == "pending" and native.get("detail", {}).get("unknown_to_system"):
                need_submit = True  # state was lost: resubmit the same intent
        resubmits = max(posts - 1, 0)
        final = native if native and native.get("status") in TERMINAL else {
            "system": self.system_id, "action_key": intent.action_key, "status": "pending",
            "detail": {"driver": "no terminal answer by horizon", "last_error": last_error}}
        record = {"action_key": intent.action_key, "copy": copy, "native": final, "claim": normalize(final).value,
                  "mapping_version": MAPPING_VERSION, "resubmits": resubmits, "accepted": accepted,
                  "arrival_rel_ms": start_rel,
                  "terminal_rel_ms": self._rel_ms() if final["status"] != "pending" else None,
                  "latency_ms": (self._rel_ms() - start_rel) if final["status"] != "pending" else None}
        self.claims.write(record)
        return record
