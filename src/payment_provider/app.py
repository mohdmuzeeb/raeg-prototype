"""Synthetic payment provider (DESIGN §5.3).

Two HTTP servers share one process and one database pool:
  * public  (PROVIDER_PORT, default 8001): /payments — the API systems under test call;
  * oracle  (ORACLE_PORT,  default 9001): /oracle/*, /admin/* — harness only, token-protected.

The provider implements the contracts declared by its profile: K1–K7 (idempotency), L1–L5
(lookup) and D (commit-time deadline). Assumption-violation switches V1–V4 deliberately break
them. Its canonical hashing is implemented independently of raeg_common so that ground truth
does not share code with the logic under test (DESIGN §11, evaluation validity).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import asyncpg
import uvicorn
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from payment_provider import store
from payment_provider.faults import FaultEngine, FaultKind, FaultPlan, FaultRule
from raeg_common.models import LookupMode, ProviderProfile

log = logging.getLogger("payment_provider")


# --------------------------------------------------------------------------- state


@dataclass
class ProviderState:
    pool: asyncpg.Pool | None = None
    profile: ProviderProfile | None = None
    faults: FaultEngine = field(default_factory=FaultEngine)
    oracle_token: str = ""
    allow_reset: bool = False

    @property
    def p(self) -> ProviderProfile:
        assert self.profile is not None, "provider profile not loaded"
        return self.profile

    @property
    def retention_ms(self) -> int | None:
        if self.p.violations.actual_retention_ms is not None:  # V3
            return self.p.violations.actual_retention_ms
        return self.p.capability.key_ttl_ms

    @property
    def actual_lookup_lag_ms(self) -> int:
        if self.p.violations.lookup_lag_exceeds_declared_ms is not None:  # V1
            return self.p.violations.lookup_lag_exceeds_declared_ms
        return self.p.behavior.actual_lookup_lag_ms

    @property
    def enforce_deadline(self) -> bool:
        return self.p.capability.deadline_enforced and not self.p.violations.ignore_deadline  # D ∧ ¬V2


STATE = ProviderState()


async def apply_config(profile: ProviderProfile, plan: FaultPlan) -> None:
    STATE.profile = profile
    STATE.faults.load(plan)
    assert STATE.pool is not None
    await store.apply_settings(STATE.pool, profile.violations.clock_offset_ms, STATE.enforce_deadline)
    await store.log_config(STATE.pool, {"profile": profile.model_dump(mode="json"), "fault_plan": plan.model_dump(mode="json")})


# --------------------------------------------------------------------------- helpers


def canonical_sha256(obj: Any) -> str:
    data = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _unit_hash(*parts: Any) -> float:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def sample_latency_ms(ref: str, ingress_index: int) -> float:
    lat = STATE.p.behavior.latency
    if lat.dist == "constant" or lat.sigma == 0:
        return lat.median_ms
    rng = random.Random(int(_unit_hash("latency", STATE.p.behavior.seed, ref, ingress_index) * 2**53))
    return lat.median_ms * math.exp(lat.sigma * rng.gauss(0.0, 1.0))


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


_REF_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
_PAYLOAD_FIELDS = {"client_reference", "amount_minor", "currency", "payer_id", "payee_id", "memo"}


def validate_payment_body(body: Any) -> str | None:
    """Return an error message, or None when valid."""
    if not isinstance(body, dict):
        return "body must be a JSON object"
    extra = set(body) - _PAYLOAD_FIELDS
    if extra:
        return f"unexpected fields: {sorted(extra)}"
    ref = body.get("client_reference")
    if not isinstance(ref, str) or not _REF_RE.match(ref):
        return "client_reference invalid"
    amount = body.get("amount_minor")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return "amount_minor must be a positive integer"
    if not isinstance(body.get("currency"), str) or not re.fullmatch(r"[A-Z]{3}", body["currency"]):
        return "currency invalid"
    for name in ("payer_id", "payee_id"):
        if not isinstance(body.get(name), str) or not body[name]:
            return f"{name} invalid"
    if not isinstance(body.get("memo", ""), str):
        return "memo invalid"
    return None


class _InjectedConnectionReset(Exception):
    """Raised after the response headers are sent, so the server aborts the connection (F1)."""


class ResetAfterHeadersResponse(Response):
    async def __call__(self, scope, receive, send) -> None:  # type: ignore[override]
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        raise _InjectedConnectionReset("LOST_RESPONSE: connection reset after headers")


class _DropInjectedResets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, _InjectedConnectionReset)


async def log_fault(receipt_id: int | None, ref: str | None, kind: str, idx: int | None, rule: FaultRule) -> None:
    assert STATE.pool is not None
    await STATE.pool.execute(
        """INSERT INTO fault_log (receipt_id, client_reference, kind, ingress_index, fault, params, applied_at)
           VALUES ($1, $2, $3, $4, $5, $6, provider_now())""",
        receipt_id,
        ref,
        kind,
        idx,
        rule.fault.value,
        rule.params,
    )


async def finish(receipt_id: int, disposition: str, status: int, payment_id: int | None = None, detail: dict | None = None) -> None:
    assert STATE.pool is not None
    await STATE.pool.execute(
        """INSERT INTO receipt_outcomes (receipt_id, finished_at, disposition, http_status, payment_id, detail)
           VALUES ($1, provider_now(), $2, $3, $4, $5)""",
        receipt_id,
        disposition,
        status,
        payment_id,
        detail,
    )


async def insert_receipt(
    request: Request, kind: str, idx: int | None, body: Any, ref: str | None, held_ms: int, deadline: datetime | None
) -> int:
    assert STATE.pool is not None
    headers = {k.lower(): v for k, v in request.headers.items()}
    return await STATE.pool.fetchval(
        """INSERT INTO receipts (arrived_at, kind, ingress_index, method, path, query, headers, body, body_sha256,
                                 client_reference, idempotency_key, deadline, attempt_hdr, system_hdr, held_ms)
           VALUES (provider_now(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) RETURNING receipt_id""",
        kind,
        idx,
        request.method,
        request.url.path,
        request.url.query or None,
        headers,
        body if isinstance(body, (dict, list)) else None,
        canonical_sha256(body) if body is not None else None,
        ref,
        headers.get("idempotency-key"),
        deadline,
        headers.get("x-raeg-attempt"),
        headers.get("x-system"),
        held_ms,
    )


def payment_record(row: asyncpg.Record) -> dict:
    return {
        "provider_txn_id": row["provider_txn_id"],
        "client_reference": row["client_reference"],
        "status": row["status"],
        "decline_code": row["decline_code"],
        "amount_minor": row["amount_minor"],
        "currency": row["currency"],
        "payer_id": row["payer_id"],
        "payee_id": row["payee_id"],
        "memo": row["memo"],
        "committed_at": row["committed_at"].isoformat(),
    }


# --------------------------------------------------------------------------- public API

public = FastAPI(title="synthetic-payment-provider", docs_url=None, redoc_url=None, openapi_url=None)


@public.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "profile": STATE.profile.id if STATE.profile else None}


@public.post("/payments")
async def create_payment(request: Request) -> Response:
    raw = await request.body()
    try:
        body: Any = json.loads(raw) if raw else None
    except ValueError:
        body = None
    ref = body.get("client_reference") if isinstance(body, dict) and isinstance(body.get("client_reference"), str) else None
    idx, faults = STATE.faults.next_ingress(ref, "dispatch")

    held_ms = 0
    if FaultKind.INGRESS_HOLD in faults:  # F9/F10: delivery delay *before* the provider sees the request
        held_ms = int(faults[FaultKind.INGRESS_HOLD].params["hold_ms"])
        await asyncio.sleep(held_ms / 1000)

    deadline_raw = request.headers.get("request-deadline")
    deadline: datetime | None = None
    deadline_error = None
    if deadline_raw is not None:
        try:
            deadline = datetime.fromisoformat(deadline_raw)
            if deadline.tzinfo is None:
                raise ValueError("naive")
        except ValueError:
            deadline, deadline_error = None, "Request-Deadline must be an ISO-8601 timestamp with offset"

    receipt_id = await insert_receipt(request, "dispatch", idx, body, ref, held_ms, deadline)
    if FaultKind.INGRESS_HOLD in faults:
        await log_fault(receipt_id, ref, "dispatch", idx, faults[FaultKind.INGRESS_HOLD])

    if deadline_error:
        await finish(receipt_id, "invalid_deadline", 422)
        return JSONResponse(error_body("INVALID_DEADLINE", deadline_error), status_code=422)
    error = validate_payment_body(body)
    if error:
        await finish(receipt_id, "invalid", 422, detail={"error": error})
        return JSONResponse(error_body("VALIDATION_ERROR", error), status_code=422)
    assert ref is not None and idx is not None

    pool = STATE.pool
    assert pool is not None
    body = {**body, "memo": body.get("memo", "")}
    body_sha = canonical_sha256(body)

    if deadline is not None and STATE.enforce_deadline:
        late = await pool.fetchval("SELECT provider_now() > $1::timestamptz", deadline)
        if late:
            await finish(receipt_id, "deadline_rejected_at_receipt", 409)
            return JSONResponse(error_body("DEADLINE_EXCEEDED", "request arrived after its deadline"), status_code=409)

    idem_key = request.headers.get("idempotency-key")
    use_idem = STATE.p.capability.idempotency and idem_key is not None
    reserved = False
    if use_idem:
        assert idem_key is not None
        waited = 0.0
        waiting_on: int | None = None
        while True:
            res = await store.reserve_key(pool, idem_key, body_sha, receipt_id, STATE.retention_ms, waiting_on)
            if res.outcome == store.ReserveOutcome.RESERVED:
                reserved = True
                break
            if res.outcome == store.ReserveOutcome.MISMATCH:
                await finish(receipt_id, "idempotency_mismatch", 422)
                return JSONResponse(
                    error_body("IDEMPOTENCY_MISMATCH", "key reused with a different body"), status_code=422
                )
            if res.outcome == store.ReserveOutcome.REPLAY:
                await finish(
                    receipt_id,
                    "replayed",
                    res.response_status or 500,
                    detail={"original_txn": (res.response_body or {}).get("provider_txn_id")},
                )
                return JSONResponse(
                    res.response_body, status_code=res.response_status or 500, headers={"Idempotent-Replayed": "true"}
                )
            # IN_PROGRESS (K2)
            if STATE.p.behavior.overlap == "reject" or waited >= STATE.p.behavior.serialize_max_wait_ms:
                await finish(receipt_id, "in_progress_409", 409, detail={"waited_ms": waited})
                return JSONResponse(
                    error_body("IDEMPOTENCY_IN_PROGRESS", "a request with this key is in progress"), status_code=409
                )
            waiting_on = res.owner_receipt_id
            await asyncio.sleep(0.02)
            waited += 20

    async def abort(disposition: str, status: int, code: str, message: str) -> Response:
        if reserved:
            assert idem_key is not None
            await store.release_key(pool, idem_key, receipt_id, disposition)
        await finish(receipt_id, disposition, status)
        return JSONResponse(error_body(code, message), status_code=status)

    await asyncio.sleep(sample_latency_ms(ref, idx) / 1000)

    if FaultKind.PRE_COMMIT_STALL in faults:  # F3
        rule = faults[FaultKind.PRE_COMMIT_STALL]
        await log_fault(receipt_id, ref, "dispatch", idx, rule)
        await asyncio.sleep(int(rule.params["stall_ms"]) / 1000)
        return await abort("stalled_no_commit", 503, "UNAVAILABLE", "processing stalled; nothing committed")
    if FaultKind.ERROR_5XX_PRE in faults:
        await log_fault(receipt_id, ref, "dispatch", idx, faults[FaultKind.ERROR_5XX_PRE])
        return await abort("error_5xx_pre", 503, "UNAVAILABLE", "nothing committed")
    if FaultKind.DELAYED_COMMIT in faults:  # F4
        rule = faults[FaultKind.DELAYED_COMMIT]
        await log_fault(receipt_id, ref, "dispatch", idx, rule)
        await asyncio.sleep(int(rule.params["delay_ms"]) / 1000)

    decline_code = None
    if FaultKind.FORCE_DECLINE in faults:
        await log_fault(receipt_id, ref, "dispatch", idx, faults[FaultKind.FORCE_DECLINE])
        decline_code = "card_declined"
    elif _unit_hash("decline", STATE.p.behavior.seed, ref) < STATE.p.behavior.decline_rate:
        decline_code = "card_declined"

    provider_txn_id = f"pay_{receipt_id:012d}"
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                if decline_code is None:
                    balance = await conn.fetchval(
                        "SELECT balance_minor FROM accounts WHERE payer_id = $1 FOR UPDATE", body["payer_id"]
                    )
                    if balance is not None:
                        if balance < body["amount_minor"]:
                            decline_code = "insufficient_funds"
                        else:
                            await conn.execute(
                                "UPDATE accounts SET balance_minor = balance_minor - $2 WHERE payer_id = $1",
                                body["payer_id"],
                                body["amount_minor"],
                            )
                status = "succeeded" if decline_code is None else "declined"
                row = await conn.fetchrow(
                    """INSERT INTO payments (provider_txn_id, client_reference, idempotency_key, status, decline_code,
                                             amount_minor, currency, payer_id, payee_id, memo, body_sha256, deadline,
                                             committed_at, receipt_id)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, provider_now(), $13) RETURNING *""",
                    provider_txn_id,
                    ref,
                    idem_key if use_idem else None,
                    status,
                    decline_code,
                    body["amount_minor"],
                    body["currency"],
                    body["payer_id"],
                    body["payee_id"],
                    body["memo"],
                    body_sha,
                    deadline,
                    receipt_id,
                )
                response_body = payment_record(row)
                http_status = 201 if status == "succeeded" else 402
                if reserved:  # K1: completion flips atomically with the payment commit
                    await conn.execute(
                        """UPDATE idempotency_keys SET state = 'COMPLETED', completed_at = provider_now(),
                                  response_status = $3, response_body = $4
                           WHERE key = $1 AND owner_receipt_id = $2""",
                        idem_key,
                        receipt_id,
                        http_status,
                        response_body,
                    )
                    await store.key_event(conn, idem_key, "completed", receipt_id, {"status": http_status})
            # the deferred deadline trigger runs here, at COMMIT
    except asyncpg.PostgresError as exc:
        if getattr(exc, "sqlstate", None) == store.DEADLINE_SQLSTATE:
            return await abort("deadline_rejected_at_commit", 409, "DEADLINE_EXCEEDED", "deadline passed before commit")
        log.exception("unexpected database error")
        return await abort("error_db", 500, "INTERNAL", "nothing committed")

    disposition = "committed" if http_status == 201 else "declined"
    lost = faults.get(FaultKind.LOST_RESPONSE)
    await finish(
        receipt_id,
        disposition,
        http_status,
        payment_id=row["payment_id"],
        detail={"fault": "LOST_RESPONSE"} if lost else None,
    )
    if lost:  # F1: the effect is committed; the client never gets a usable response
        await log_fault(receipt_id, ref, "dispatch", idx, lost)
        if lost.params.get("mode", "reset") == "blackhole":
            await asyncio.sleep(int(lost.params.get("blackhole_ms", 60_000)) / 1000)
        else:
            payload = json.dumps(response_body).encode()
            return ResetAfterHeadersResponse(content=payload, status_code=http_status, media_type="application/json")
    return JSONResponse(response_body, status_code=http_status)


async def _lookup_preamble(request: Request, kind: str, ref: str | None) -> tuple[int, Response | None]:
    idx, faults = STATE.faults.next_ingress(ref, kind) if kind == "lookup_ref" else (None, {})
    receipt_id = await insert_receipt(request, kind, idx, None, ref, 0, None)
    if FaultKind.LOOKUP_UNAVAILABLE in faults:  # F8
        rule = faults[FaultKind.LOOKUP_UNAVAILABLE]
        await log_fault(receipt_id, ref, kind, idx, rule)
        status = int(rule.params.get("status", 503))
        await finish(receipt_id, "lookup_unavailable", status)
        return receipt_id, JSONResponse(error_body("UNAVAILABLE", "lookup unavailable"), status_code=status)
    return receipt_id, None


@public.get("/payments")
async def lookup_by_reference(request: Request, client_reference: str) -> Response:
    receipt_id, early = await _lookup_preamble(request, "lookup_ref", client_reference)
    if early is not None:
        return early
    if STATE.p.capability.lookup != LookupMode.BY_CLIENT_REF:
        await finish(receipt_id, "lookup_not_supported", 501)
        return JSONResponse(error_body("NOT_SUPPORTED", "lookup by client_reference not supported"), status_code=501)
    assert STATE.pool is not None
    rows = await STATE.pool.fetch(
        """WITH n AS (SELECT provider_now() AS as_of)
           SELECT n.as_of, p.* FROM n
           LEFT JOIN payments p ON p.client_reference = $1
                               AND p.committed_at <= n.as_of - make_interval(secs => $2 / 1000.0)
           ORDER BY p.payment_id""",
        client_reference,
        STATE.actual_lookup_lag_ms,
    )
    as_of = rows[0]["as_of"]
    records = [payment_record(r) for r in rows if r["payment_id"] is not None]
    await finish(receipt_id, "lookup_ok", 200, detail={"n_records": len(records), "as_of": as_of.isoformat()})
    return JSONResponse({"as_of": as_of.isoformat(), "client_reference": client_reference, "records": records})


@public.get("/payments/{provider_txn_id}")
async def lookup_by_id(request: Request, provider_txn_id: str) -> Response:
    receipt_id, _ = await _lookup_preamble(request, "lookup_id", None)
    if STATE.p.capability.lookup == LookupMode.NONE:
        await finish(receipt_id, "lookup_not_supported", 501)
        return JSONResponse(error_body("NOT_SUPPORTED", "lookup not supported"), status_code=501)
    assert STATE.pool is not None
    rows = await STATE.pool.fetch(
        """WITH n AS (SELECT provider_now() AS as_of)
           SELECT n.as_of, p.* FROM n
           LEFT JOIN payments p ON p.provider_txn_id = $1
                               AND p.committed_at <= n.as_of - make_interval(secs => $2 / 1000.0)""",
        provider_txn_id,
        STATE.actual_lookup_lag_ms,
    )
    as_of = rows[0]["as_of"]
    record = payment_record(rows[0]) if rows[0]["payment_id"] is not None else None
    await finish(receipt_id, "lookup_ok", 200, detail={"found": record is not None})
    return JSONResponse({"as_of": as_of.isoformat(), "record": record})


# --------------------------------------------------------------------------- oracle / admin API

oracle = FastAPI(title="provider-oracle", docs_url=None, redoc_url=None, openapi_url=None)


def require_token(x_oracle_token: str = Header(default="")) -> None:
    if not STATE.oracle_token or x_oracle_token != STATE.oracle_token:
        raise HTTPException(status_code=403, detail="oracle token required")


@oracle.get("/healthz")
async def oracle_healthz() -> dict:
    return {"ok": True}


async def _rows(sql: str, *args: Any) -> list[dict]:
    assert STATE.pool is not None
    out = []
    for row in await STATE.pool.fetch(sql, *args):
        out.append({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(row).items()})
    return out


@oracle.get("/oracle/now", dependencies=[Depends(require_token)])
async def oracle_now() -> dict:
    assert STATE.pool is not None
    return {"provider_now": (await STATE.pool.fetchval("SELECT provider_now()")).isoformat()}


@oracle.get("/oracle/ledger", dependencies=[Depends(require_token)])
async def oracle_ledger() -> list[dict]:
    return await _rows("SELECT * FROM payments ORDER BY payment_id")


@oracle.get("/oracle/receipts", dependencies=[Depends(require_token)])
async def oracle_receipts() -> list[dict]:
    return await _rows(
        """SELECT r.*, o.finished_at, o.disposition, o.http_status, o.payment_id, o.detail
           FROM receipts r LEFT JOIN receipt_outcomes o USING (receipt_id) ORDER BY r.receipt_id"""
    )


@oracle.get("/oracle/keys", dependencies=[Depends(require_token)])
async def oracle_keys() -> dict:
    return {
        "keys": await _rows("SELECT * FROM idempotency_keys ORDER BY key"),
        "events": await _rows("SELECT * FROM key_events ORDER BY id"),
    }


@oracle.get("/oracle/faults", dependencies=[Depends(require_token)])
async def oracle_faults() -> list[dict]:
    return await _rows("SELECT * FROM fault_log ORDER BY id")


@oracle.get("/oracle/config", dependencies=[Depends(require_token)])
async def oracle_config() -> dict:
    return {
        "profile": STATE.p.model_dump(mode="json"),
        "fault_plan": STATE.faults.plan.model_dump(mode="json"),
        "effective": {
            "retention_ms": STATE.retention_ms,
            "actual_lookup_lag_ms": STATE.actual_lookup_lag_ms,
            "enforce_deadline": STATE.enforce_deadline,
            "clock_offset_ms": STATE.p.violations.clock_offset_ms,
        },
        "config_log": await _rows("SELECT * FROM config_log ORDER BY id"),
    }


@oracle.post("/admin/config", dependencies=[Depends(require_token)])
async def admin_config(payload: dict) -> dict:
    profile = ProviderProfile.model_validate(payload["profile"])
    plan = FaultPlan.model_validate(payload.get("fault_plan") or {})
    await apply_config(profile, plan)
    return {"ok": True, "profile": profile.id, "plan_id": plan.plan_id}


@oracle.post("/admin/reset", dependencies=[Depends(require_token)])
async def admin_reset() -> dict:
    if not STATE.allow_reset:
        raise HTTPException(status_code=403, detail="reset disabled (PROVIDER_ALLOW_RESET != 1)")
    assert STATE.pool is not None
    await STATE.pool.execute(
        """TRUNCATE receipts, receipt_outcomes, payments, idempotency_keys, key_events, accounts, fault_log, config_log
           RESTART IDENTITY CASCADE"""
    )
    STATE.faults.reset_counters()
    await apply_config(STATE.p, STATE.faults.plan)
    return {"ok": True}


@oracle.post("/admin/accounts", dependencies=[Depends(require_token)])
async def admin_accounts(accounts: list[dict]) -> dict:
    assert STATE.pool is not None
    for acct in accounts:
        await STATE.pool.execute(
            """INSERT INTO accounts (payer_id, balance_minor) VALUES ($1, $2)
               ON CONFLICT (payer_id) DO UPDATE SET balance_minor = EXCLUDED.balance_minor""",
            acct["payer_id"],
            int(acct["balance_minor"]),
        )
    return {"ok": True, "n": len(accounts)}


@oracle.post("/admin/recover", dependencies=[Depends(require_token)])
async def admin_recover() -> dict:
    """Run the K7 startup recovery. Only valid while no request is in flight (tests)."""
    assert STATE.pool is not None
    return {"released": await store.recover_stuck_reservations(STATE.pool)}


# --------------------------------------------------------------------------- entry point


def _load_initial_profile() -> ProviderProfile:
    path = os.environ.get("PROVIDER_PROFILE")
    if not path:
        raise SystemExit("PROVIDER_PROFILE must point to a profile YAML")
    with open(path, encoding="utf-8") as fh:
        return ProviderProfile.model_validate(yaml.safe_load(fh))


def _load_initial_plan() -> FaultPlan:
    path = os.environ.get("PROVIDER_FAULT_PLAN")
    if not path:
        return FaultPlan()
    with open(path, encoding="utf-8") as fh:
        return FaultPlan.model_validate(json.load(fh))


async def serve() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("uvicorn.error").addFilter(_DropInjectedResets())
    STATE.oracle_token = os.environ["ORACLE_TOKEN"]
    STATE.allow_reset = os.environ.get("PROVIDER_ALLOW_RESET") == "1"
    STATE.pool = await store.create_pool(os.environ["PROVIDER_DB_DSN"])
    applied = await store.migrate(STATE.pool)
    released = await store.recover_stuck_reservations(STATE.pool)  # K7
    await apply_config(_load_initial_profile(), _load_initial_plan())
    log.info("provider ready profile=%s migrations=%s released_on_recovery=%d", STATE.p.id, applied, released)

    def make(app: FastAPI, port: int) -> uvicorn.Server:
        return uvicorn.Server(
            uvicorn.Config(app, host="0.0.0.0", port=port, http="h11", loop="asyncio", log_level="warning",
                           access_log=False, timeout_keep_alive=30)
        )

    servers = [make(public, int(os.environ.get("PROVIDER_PORT", 8001))), make(oracle, int(os.environ.get("ORACLE_PORT", 9001)))]
    await asyncio.gather(*(s.serve() for s in servers))


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
