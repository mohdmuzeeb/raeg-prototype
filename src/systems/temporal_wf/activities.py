"""Temporal activities: one provider dispatch, one lookup. Both use the shared provider client, so
transport, hooks and exchange logging are identical to every other system (DESIGN §5.4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from temporalio import activity
from temporalio.exceptions import ApplicationError

from raeg_common.models import Observation, PaymentPayload
from raeg_common.provider_client import ProviderClient
from systems.base import load_systems, make_client

_CLIENTS: dict[str, ProviderClient] = {}


def client_for(system_id: str) -> ProviderClient:
    if system_id not in _CLIENTS:
        _CLIENTS[system_id] = make_client(load_systems()[system_id], worker_tag="-worker")
    return _CLIENTS[system_id]


@dataclass
class DispatchInput:
    system_id: str
    action_key: str
    payload: dict
    attempt_ms: int
    margin_ms: int
    idempotency_key: str | None
    deadline_iso: str | None = None  # B3L: fixed by the workflow; B3: per activity attempt
    attempt_no: int | None = None  # B3L: workflow loop counter; B3: Temporal's attempt number


@activity.defn
async def dispatch_payment(inp: DispatchInput) -> dict:
    now = datetime.now(UTC)
    deadline = datetime.fromisoformat(inp.deadline_iso) if inp.deadline_iso else now + timedelta(milliseconds=inp.attempt_ms)
    attempt_no = inp.attempt_no or activity.info().attempt
    timeout = max((deadline - now).total_seconds() - inp.margin_ms / 1000, 0.05)
    r = await client_for(inp.system_id).dispatch(
        inp.action_key, PaymentPayload.model_validate(inp.payload), attempt_no, deadline, inp.idempotency_key,
        timeout_s=timeout)
    if r.observation == Observation.COMMITTED:
        return {"provider_txn_id": r.provider_txn_id, "replayed": r.replayed, "attempt_no": attempt_no}
    if r.observation == Observation.DECLINED:
        raise ApplicationError("declined by provider", {"http_status": r.http_status}, type="Declined", non_retryable=True)
    if r.observation == Observation.INTERNAL:
        raise ApplicationError("idempotency mismatch", type="Internal", non_retryable=True)
    raise ApplicationError(f"ambiguous: {r.error_class or r.observation.value}", type=r.observation.value)


@dataclass
class LookupInput:
    system_id: str
    action_key: str
    timeout_ms: int


@activity.defn
async def lookup_payment(inp: LookupInput) -> dict:
    r = await client_for(inp.system_id).lookup_by_reference(inp.action_key, timeout_s=inp.timeout_ms / 1000)
    if not r.ok:
        raise ApplicationError(f"lookup failed: {r.error_class}", type="LookupError",
                               non_retryable=r.http_status == 501)
    return {"as_of": r.as_of, "records": r.records}
