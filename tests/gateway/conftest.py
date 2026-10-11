"""Fixtures for M3 gateway-store tests. Requires raeg-db from `make up-test` (127.0.0.1:55432)."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

from raeg_gateway.store import GatewayStore, create_pool, migrate

# Tests use their own database (raeg_test) on the raeg-db server: the live `systems` service keeps
# `raeg`, so test TRUNCATEs never contend with its background loops (deadlock seen in run
# 20261011T035247Z_gateway).
SERVER_DSN = os.environ.get("RAEG_DB_SERVER_DSN", "postgresql://postgres:postgres@127.0.0.1:55432/postgres")
ADMIN_DSN = os.environ.get("RAEG_DB_ADMIN_DSN", "postgresql://postgres:postgres@127.0.0.1:55432/raeg_test")
APP_DSN = os.environ.get("RAEG_DB_APP_DSN", "postgresql://raeg_app:raeg_app@127.0.0.1:55432/raeg_test")
TABLES = "executions, submissions, attempts, evidence, decisions, execution_events, anomalies"

pytestmark = pytest.mark.gateway

PAYLOAD = {"amount_minor": 1000, "currency": "USD", "payer_id": "payer-1", "payee_id": "merchant-1", "memo": ""}
CAPABILITY = {"idempotency": True, "key_ttl_ms": None, "lookup": "by_client_ref", "lookup_lag_ms": 500, "deadline_enforced": True}
POLICY = {"mode": "STRICT", "max_dispatch": 3, "max_verify": 3}


async def ensure_test_database() -> None:
    server = await asyncpg.connect(SERVER_DSN)
    try:
        if not await server.fetchval("SELECT 1 FROM pg_database WHERE datname = 'raeg_test'"):
            await server.execute("CREATE DATABASE raeg_test")
    finally:
        await server.close()


@pytest.fixture
async def admin():
    try:
        await ensure_test_database()
        conn = await asyncpg.connect(ADMIN_DSN)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.fail(f"raeg-db not reachable — run `make up-test` first ({exc})")
    await migrate(ADMIN_DSN)
    await conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    yield conn
    await conn.close()


@pytest.fixture
async def store(admin):
    pool = await create_pool(APP_DSN, max_size=60)
    yield GatewayStore(pool, code_sha="test", policy_version="test-policy")
    await pool.close()


@pytest.fixture
async def app_conn(admin):
    conn = await asyncpg.connect(APP_DSN)
    yield conn
    await conn.close()


def new_key() -> str:
    return f"a-{uuid.uuid4().hex[:12]}"


async def submit(store: GatewayStore, key: str | None = None, payload: dict | None = None, fp: str = "fp-1"):
    key = key or new_key()
    result = await store.submit(key, payload or PAYLOAD, fp, "P1", CAPABILITY, POLICY, "w-submit")
    return key, result
