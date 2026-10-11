"""Fixtures for the provider contract conformance suite (DESIGN §9, M2).

Requires the stack started with `make up-test` (PROVIDER_ALLOW_RESET=1). All times used in
assertions and deadlines come from the provider clock (/oracle/now), never from the host clock:
the colima VM clock is not synchronised with the macOS host.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest

from raeg_common.models import ProviderProfile
from raeg_common.profiles import load_all

PUBLIC_URL = os.environ.get("PROVIDER_URL", "http://127.0.0.1:18001")
ORACLE_URL = os.environ.get("ORACLE_URL", "http://127.0.0.1:19001")
ORACLE_TOKEN = os.environ.get("ORACLE_TOKEN", "local-oracle-token")
PROVIDER_DB_DSN = os.environ.get("PROVIDER_DB_DSN_HOST", "postgresql://postgres:postgres@127.0.0.1:55433/provider")

PROFILES = load_all()

pytestmark = pytest.mark.conformance


def profile(pid: str, **overrides: Any) -> ProviderProfile:
    """A shipped profile with nested overrides, e.g. profile('P2', behavior={'overlap': 'serialize'})."""
    data = PROFILES[pid].model_dump(mode="json")
    for section, values in overrides.items():
        if isinstance(values, dict):
            data[section] = {**data.get(section, {}), **values}
        else:
            data[section] = values
    return ProviderProfile.model_validate(data)


def body(ref: str, amount: int = 1000, payer: str = "payer-1") -> dict:
    return {"client_reference": ref, "amount_minor": amount, "currency": "USD", "payer_id": payer, "payee_id": "merchant-1", "memo": ""}


@dataclass
class PayResult:
    status: int | None
    json: Any
    headers: dict
    error: str | None

    @property
    def replayed(self) -> bool:
        return self.headers.get("idempotent-replayed") == "true"

    @property
    def code(self) -> str | None:
        return (self.json or {}).get("error", {}).get("code") if isinstance(self.json, dict) else None


class Provider:
    def __init__(self) -> None:
        self.http = httpx.AsyncClient(base_url=PUBLIC_URL, timeout=30)
        self.admin = httpx.AsyncClient(base_url=ORACLE_URL, timeout=30, headers={"x-oracle-token": ORACLE_TOKEN})
        self.profile: ProviderProfile | None = None

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.admin.aclose()

    async def configure(self, prof: ProviderProfile, rules: list[dict] | None = None) -> None:
        r = await self.admin.post("/admin/reset")
        assert r.status_code == 200, f"reset failed (stack must run with PROVIDER_ALLOW_RESET=1): {r.text}"
        r = await self.admin.post(
            "/admin/config",
            json={"profile": prof.model_dump(mode="json"), "fault_plan": {"plan_id": "conformance", "rules": rules or []}},
        )
        assert r.status_code == 200, r.text
        r = await self.admin.post("/admin/reset")  # restart counters/ids under the new config
        assert r.status_code == 200
        self.profile = prof

    async def pay(
        self,
        ref: str,
        *,
        key: str | None = None,
        deadline: datetime | None = None,
        amount: int = 1000,
        payer: str = "payer-1",
        timeout: float = 30.0,
        extra_headers: dict | None = None,
    ) -> PayResult:
        headers = {"x-system": "conformance", **(extra_headers or {})}
        if key is not None:
            headers["Idempotency-Key"] = key
        if deadline is not None:
            headers["Request-Deadline"] = deadline.isoformat(timespec="milliseconds")
        try:
            r = await self.http.post("/payments", json=body(ref, amount, payer), headers=headers, timeout=timeout)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return PayResult(None, None, {}, type(exc).__name__)
        try:
            parsed = r.json()
        except ValueError:
            parsed = None
        return PayResult(r.status_code, parsed, {k.lower(): v for k, v in r.headers.items()}, None)

    async def lookup(self, ref: str) -> httpx.Response:
        return await self.http.get("/payments", params={"client_reference": ref})

    async def lookup_id(self, txn: str) -> httpx.Response:
        return await self.http.get(f"/payments/{txn}")

    async def now(self) -> datetime:
        r = await self.admin.get("/oracle/now")
        return datetime.fromisoformat(r.json()["provider_now"])

    async def sleep_until(self, target: datetime) -> None:
        while True:
            remaining = (target - await self.now()).total_seconds()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 0.25))

    async def ledger(self, ref: str | None = None, status: str | None = "succeeded") -> list[dict]:
        rows = (await self.admin.get("/oracle/ledger")).json()
        return [r for r in rows if (ref is None or r["client_reference"] == ref) and (status is None or r["status"] == status)]

    async def receipts(self, ref: str | None = None, kind: str = "dispatch") -> list[dict]:
        rows = (await self.admin.get("/oracle/receipts")).json()
        return [r for r in rows if (ref is None or r["client_reference"] == ref) and r["kind"] == kind]

    async def keys(self) -> dict:
        return (await self.admin.get("/oracle/keys")).json()

    async def key_created_at(self, key: str) -> datetime:
        (row,) = [k for k in (await self.keys())["keys"] if k["key"] == key]
        return datetime.fromisoformat(row["created_at"])

    async def faults(self) -> list[dict]:
        return (await self.admin.get("/oracle/faults")).json()


@pytest.fixture
async def provider():
    p = Provider()
    try:
        r = await p.admin.get("/healthz")
    except httpx.TransportError as exc:
        pytest.fail(f"provider stack not reachable at {ORACLE_URL} — run `make up-test` first ({exc})")
    assert r.status_code == 200
    yield p
    await p.aclose()


@pytest.fixture
async def provider_db():
    conn = await asyncpg.connect(PROVIDER_DB_DSN)
    yield conn
    await conn.close()


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def ms(n: float) -> timedelta:
    return timedelta(milliseconds=n)
