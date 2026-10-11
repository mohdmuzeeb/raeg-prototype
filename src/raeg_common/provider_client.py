"""Shared provider client used by RAEG and every baseline (DESIGN §5.4).

It owns transport settings, the classification of responses into observations (§2.9),
the crash/pause hooks (§4) and the raw exchange log (§7). Systems differ only in what they
do with the returned observation.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from raeg_common.exchange_log import ExchangeLog, utc_now_iso
from raeg_common.hooks import HookAction, HookPlan, HookPoint
from raeg_common.models import Observation, PaymentPayload

DEADLINE_HEADER = "Request-Deadline"
ATTEMPT_HEADER = "X-RAEG-Attempt"
SYSTEM_HEADER = "X-System"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"


def format_deadline(deadline: datetime) -> str:
    if deadline.tzinfo is None:
        raise ValueError("deadline must be timezone-aware")
    return deadline.isoformat(timespec="milliseconds")


@dataclass
class DispatchResult:
    observation: Observation
    http_status: int | None
    body: Any
    headers: dict[str, str]
    error_class: str | None
    sent_at: str
    received_at: str
    latency_ms: float
    replayed: bool = False
    provider_txn_id: str | None = None
    hook: dict | None = None


@dataclass
class LookupResult:
    ok: bool
    as_of: str | None
    records: list[dict] = field(default_factory=list)
    http_status: int | None = None
    error_class: str | None = None
    latency_ms: float = 0.0


def classify(status: int, body: Any) -> Observation:
    """Map an HTTP response to a per-attempt observation (DESIGN §2.9). Conservative default."""
    code = body.get("error", {}).get("code") if isinstance(body, dict) else None
    if status in (200, 201) and isinstance(body, dict) and body.get("status") == "succeeded":
        return Observation.COMMITTED
    if status == 402:
        return Observation.DECLINED
    if status == 422:
        return Observation.INTERNAL if code == "IDEMPOTENCY_MISMATCH" else Observation.DECLINED
    if status == 409 and code == "DEADLINE_EXCEEDED":
        return Observation.DEADLINE_REJECTED
    # 409 IDEMPOTENCY_IN_PROGRESS, 5xx, and anything unexpected: outcome unknown.
    return Observation.AMBIGUOUS


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"_raw_text": response.text}


class ProviderClient:
    def __init__(
        self,
        base_url: str,
        system_id: str,
        timeout_s: float,
        hooks: HookPlan | None = None,
        log: ExchangeLog | None = None,
        send_deadline: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.system_id = system_id
        self.send_deadline = send_deadline  # False for the /noDL controls (DESIGN §5.4)
        self._hooks = hooks or HookPlan()
        self._log = log or ExchangeLog(None)
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s),
            transport=transport,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def dispatch(
        self,
        action_key: str,
        payload: PaymentPayload,
        attempt_no: int,
        deadline: datetime | None,
        idempotency_key: str | None,
    ) -> DispatchResult:
        headers = {SYSTEM_HEADER: self.system_id, ATTEMPT_HEADER: str(attempt_no)}
        if idempotency_key is not None:
            headers[IDEMPOTENCY_HEADER] = idempotency_key
        if deadline is not None and self.send_deadline:
            headers[DEADLINE_HEADER] = format_deadline(deadline)
        body = {"client_reference": action_key, **payload.model_dump()}
        hook = self._hooks.lookup(action_key, attempt_no)
        base = {
            "kind": "dispatch",
            "system": self.system_id,
            "action_key": action_key,
            "attempt_no": attempt_no,
            "request": {"method": "POST", "path": "/payments", "headers": headers, "body": body},
            "hook": hook.to_json() if hook else None,
        }

        if hook and hook.point == HookPoint.CP1:
            self._log.write({**base, "event": "hook_fired", "point": "CP1"})
            self._hooks.crash()
        if hook and hook.point == HookPoint.PAUSE:
            self._log.write({**base, "event": "pause_begin", "pause_ms": hook.pause_ms})
            await HookPlan.pause(hook)
            self._log.write({**base, "event": "pause_end"})

        sent_at = utc_now_iso()
        t0 = time.monotonic()
        request = self._http.post("/payments", json=body, headers=headers)
        try:
            if hook and hook.point == HookPoint.CP2:
                response = await self._send_then_crash(request, hook, base)
            else:
                response = await request
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            latency = (time.monotonic() - t0) * 1000
            result = DispatchResult(
                observation=Observation.AMBIGUOUS,
                http_status=None,
                body=None,
                headers={},
                error_class=type(exc).__name__,
                sent_at=sent_at,
                received_at=utc_now_iso(),
                latency_ms=latency,
                hook=base["hook"],
            )
            self._log.write({**base, "event": "exchange", **_result_log(result), "error": str(exc)})
            return result

        latency = (time.monotonic() - t0) * 1000
        parsed = _safe_json(response)
        observation = classify(response.status_code, parsed)
        result = DispatchResult(
            observation=observation,
            http_status=response.status_code,
            body=parsed,
            headers=dict(response.headers),
            error_class=None,
            sent_at=sent_at,
            received_at=utc_now_iso(),
            latency_ms=latency,
            replayed=response.headers.get(REPLAY_HEADER, "").lower() == "true",
            provider_txn_id=parsed.get("provider_txn_id") if isinstance(parsed, dict) else None,
            hook=base["hook"],
        )
        self._log.write({**base, "event": "exchange", **_result_log(result)})
        if hook and hook.point == HookPoint.CP3:
            self._log.write({**base, "event": "hook_fired", "point": "CP3"})
            self._hooks.crash()
        return result

    async def _send_then_crash(self, request, hook: HookAction, base: dict) -> httpx.Response:
        task = asyncio.ensure_future(request)
        done, _ = await asyncio.wait({task}, timeout=hook.after_send_ms / 1000)
        if done:  # the response beat the crash point; still crash before returning it
            self._log.write({**base, "event": "hook_fired", "point": "CP2", "note": "response_already_received"})
        else:
            self._log.write({**base, "event": "hook_fired", "point": "CP2"})
        self._hooks.crash()
        return await task  # reached only when exit_fn does not terminate (tests)

    async def lookup_by_reference(self, action_key: str) -> LookupResult:
        return await self._lookup("/payments", {"client_reference": action_key}, action_key)

    async def lookup_by_provider_id(self, provider_txn_id: str, action_key: str) -> LookupResult:
        return await self._lookup(f"/payments/{provider_txn_id}", None, action_key)

    async def _lookup(self, path: str, params: dict | None, action_key: str) -> LookupResult:
        headers = {SYSTEM_HEADER: self.system_id}
        base = {
            "kind": "lookup",
            "system": self.system_id,
            "action_key": action_key,
            "request": {"method": "GET", "path": path, "params": params, "headers": headers},
        }
        t0 = time.monotonic()
        try:
            response = await self._http.get(path, params=params, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            result = LookupResult(
                ok=False, as_of=None, error_class=type(exc).__name__, latency_ms=(time.monotonic() - t0) * 1000
            )
            self._log.write({**base, "event": "exchange", "ok": False, "error_class": result.error_class, "error": str(exc)})
            return result
        parsed = _safe_json(response)
        ok = response.status_code == 200 and isinstance(parsed, dict) and "as_of" in parsed
        records = []
        if ok:
            records = parsed.get("records", [parsed.get("record")] if parsed.get("record") else [])
        result = LookupResult(
            ok=ok,
            as_of=parsed.get("as_of") if isinstance(parsed, dict) else None,
            records=records,
            http_status=response.status_code,
            error_class=None if ok else f"HTTP_{response.status_code}",
            latency_ms=(time.monotonic() - t0) * 1000,
        )
        self._log.write(
            {
                **base,
                "event": "exchange",
                "ok": ok,
                "response": {"status": response.status_code, "headers": dict(response.headers), "body": parsed},
                "latency_ms": result.latency_ms,
            }
        )
        return result


def _result_log(result: DispatchResult) -> dict:
    return {
        "observation": result.observation.value,
        "response": {"status": result.http_status, "headers": result.headers, "body": result.body},
        "error_class": result.error_class,
        "sent_at": result.sent_at,
        "received_at": result.received_at,
        "latency_ms": result.latency_ms,
        "replayed": result.replayed,
    }
