"""Fixtures for M4 integration tests: real raeg-db + real provider container + in-process engine.

Requires `make up-test`. Behavioural checks only: these tests assert outcomes of specific,
deterministic fault scenarios; they produce no performance measurements.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from raeg_common.exchange_log import ExchangeLog
from raeg_common.models import PaymentPayload
from raeg_common.provider_client import ProviderClient
from raeg_gateway.audit import check_invariants
from raeg_gateway.controller import Policy
from raeg_gateway.engine import RecoveryEngine
from raeg_gateway.store import GatewayStore, create_pool
from raeg_gateway.transitions import State
from tests.conformance.conftest import PUBLIC_URL, Provider, profile
from tests.gateway.conftest import ADMIN_DSN, APP_DSN, admin  # noqa: F401  (fixture re-export)

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = PaymentPayload(amount_minor=1000, currency="USD", payer_id="payer-1", payee_id="merchant-1")
pytestmark = pytest.mark.integration


@pytest.fixture
async def provider():
    p = Provider()
    yield p
    await p.aclose()


@pytest.fixture
async def store(admin):  # noqa: F811
    pool = await create_pool(APP_DSN, max_size=30)
    yield GatewayStore(pool, code_sha=os.environ.get("RAEG_CODE_SHA", "test"), policy_version="it")
    await pool.close()


@pytest.fixture
async def engine(store, tmp_path):
    client = ProviderClient(PUBLIC_URL, "raeg-it", timeout_s=10, log=ExchangeLog(str(tmp_path / "exchanges.jsonl")))
    yield RecoveryEngine(store, client, f"engine-{uuid.uuid4().hex[:6]}")
    await client.aclose()


@dataclass
class Outcome:
    key: str
    state: State
    effects: int
    row: dict
    rules: list[str]
    attempts: int


async def outcome(provider: Provider, store: GatewayStore, key: str) -> Outcome:
    row = await store.snapshot(key)
    rules = [r["rule_id"] for r in await store.pool.fetch(
        "SELECT rule_id FROM decisions WHERE action_key = $1 AND applied ORDER BY id", key)]
    return Outcome(key, State(row["state"]), len(await provider.ledger(key)), row, rules, row["attempt_count"])


async def scenario(provider, store, engine, pid: str, policy: Policy, rules=None, *, profile_overrides=None,
                   accounts=None, key=None) -> Outcome:
    key = key or f"it-{uuid.uuid4().hex[:10]}"
    prof = profile(pid, **(profile_overrides or {}))
    await provider.configure(prof, [{**r, "client_reference": key} for r in (rules or [])])
    if accounts:
        await provider.admin.post("/admin/accounts", json=accounts)
    await engine.submit(key, PAYLOAD, pid, prof.capability, policy)
    await engine.run_to_quiescence(key, max_steps=400, poll_s=0.05)
    await asyncio.sleep(0.3)
    await engine.consume_late_evidence()
    return await outcome(provider, store, key)


async def assert_audit_clean(admin_conn) -> None:
    report = await check_invariants(admin_conn)
    assert {k: v for k, v in report.items() if v} == {}


async def assert_decisions_recorded(admin_conn) -> None:
    """Every applied decision carries a rule id and a full input snapshot; lookups cite their evidence."""
    rows = await admin_conn.fetch("SELECT rule_id, transition, inputs, evidence_ids FROM decisions WHERE applied")
    assert rows
    for r in rows:
        inputs = json.loads(r["inputs"]) if isinstance(r["inputs"], str) else r["inputs"]
        assert r["rule_id"], r
        if r["transition"] not in ("T1", "T2", "T13", "T13a") and r["rule_id"] != "SWEEP_LEASE_EXPIRED":
            for k in ("snapshot", "capability", "policy", "now"):
                assert k in inputs, (r["rule_id"], sorted(inputs))
        if r["rule_id"].startswith(("L-", "D-")):
            assert list(r["evidence_ids"]), r["rule_id"]


def run_worker_process(key: str, hooks: list[dict], tmp_path: Path, worker_id: str = "crashing-worker",
                       wait: bool = True) -> subprocess.Popen | subprocess.CompletedProcess:
    plan = tmp_path / f"hooks-{worker_id}.json"
    plan.write_text(json.dumps({"hooks": [{**h, "action_key": key} for h in hooks]}))
    cmd = [sys.executable, "-m", "raeg_gateway.worker_cli", "--app-dsn", APP_DSN, "--provider-url", PUBLIC_URL,
           "--worker-id", worker_id, "--action", key, "--hooks", str(plan),
           "--exchange-log", str(tmp_path / f"exchanges-{worker_id}.jsonl")]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    if wait:
        return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    return subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
