"""Database access for the synthetic provider: migrations, settings, idempotency records."""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
DEADLINE_SQLSTATE = "RA001"
APPEND_ONLY_SQLSTATE = "RA002"


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=2, max_size=40, init=_init_conn)


async def migrate(pool: asyncpg.Pool) -> list[str]:
    applied = []
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock(424242)")
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.stem in done:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.stem)
                applied.append(path.stem)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(424242)")
    return applied


async def apply_settings(pool: asyncpg.Pool, clock_offset_ms: int, enforce_deadline: bool) -> None:
    await pool.execute(
        "UPDATE provider_settings SET clock_offset_ms = $1, enforce_deadline = $2 WHERE id = 1",
        clock_offset_ms,
        enforce_deadline,
    )


async def log_config(pool: asyncpg.Pool, config: dict[str, Any]) -> None:
    await pool.execute("INSERT INTO config_log (at, config) VALUES (provider_now(), $1)", config)


async def key_event(conn: asyncpg.Connection, key: str, event: str, receipt_id: int | None, detail: dict | None = None) -> None:
    await conn.execute(
        "INSERT INTO key_events (key, event, receipt_id, at, detail) VALUES ($1, $2, $3, provider_now(), $4)",
        key,
        event,
        receipt_id,
        detail,
    )


class ReserveOutcome(enum.Enum):
    RESERVED = "reserved"
    REPLAY = "replay"
    IN_PROGRESS = "in_progress"
    MISMATCH = "mismatch"


@dataclass
class Reservation:
    outcome: ReserveOutcome
    response_status: int | None = None
    response_body: dict | None = None
    created_at: Any = None
    owner_receipt_id: int | None = None


async def reserve_key(
    pool: asyncpg.Pool,
    key: str,
    body_sha256: str,
    receipt_id: int,
    retention_ms: int | None,
    waiting_on_owner: int | None = None,
) -> Reservation:
    """One reservation step (K1–K5). Lazy expiry happens in the same transaction as the lookup.

    `waiting_on_owner` is set by a serialize-mode waiter (K2): the record it waited on is replayed
    once COMPLETED even if retention elapsed meanwhile, because the waiter overlapped it in flight.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """SELECT k.*, provider_now() AS now FROM idempotency_keys k WHERE key = $1 FOR UPDATE""", key
            )
            waited_on = row is not None and waiting_on_owner is not None and row["owner_receipt_id"] == waiting_on_owner
            if row is not None and row["state"] == "COMPLETED" and retention_ms is not None and not waited_on:
                expired = await conn.fetchval(
                    "SELECT provider_now() >= $1::timestamptz + make_interval(secs => $2 / 1000.0)",
                    row["created_at"],
                    retention_ms,
                )
                if expired:
                    await conn.execute("DELETE FROM idempotency_keys WHERE key = $1", key)
                    await key_event(
                        conn, key, "expired", receipt_id, {"created_at": row["created_at"].isoformat(), "retention_ms": retention_ms}
                    )
                    row = None
            if row is None:
                inserted = await conn.fetchrow(
                    """INSERT INTO idempotency_keys (key, body_sha256, state, created_at, owner_receipt_id)
                       VALUES ($1, $2, 'IN_PROGRESS', provider_now(), $3)
                       ON CONFLICT (key) DO NOTHING RETURNING created_at""",
                    key,
                    body_sha256,
                    receipt_id,
                )
                if inserted is not None:
                    await key_event(conn, key, "reserved", receipt_id)
                    return Reservation(ReserveOutcome.RESERVED, created_at=inserted["created_at"])
                # A concurrent first request won the insert; treat as in progress (caller may retry).
                await key_event(conn, key, "in_progress", receipt_id, {"race": "concurrent_insert"})
                return Reservation(ReserveOutcome.IN_PROGRESS)
            if row["body_sha256"] != body_sha256:
                await key_event(conn, key, "mismatch", receipt_id)
                return Reservation(ReserveOutcome.MISMATCH)
            if row["state"] == "COMPLETED":
                await key_event(conn, key, "replayed", receipt_id, {"owner_receipt_id": row["owner_receipt_id"]})
                return Reservation(
                    ReserveOutcome.REPLAY,
                    response_status=row["response_status"],
                    response_body=row["response_body"],
                    created_at=row["created_at"],
                )
            await key_event(conn, key, "in_progress", receipt_id, {"owner_receipt_id": row["owner_receipt_id"]})
            return Reservation(ReserveOutcome.IN_PROGRESS, created_at=row["created_at"], owner_receipt_id=row["owner_receipt_id"])


async def release_key(pool: asyncpg.Pool, key: str, receipt_id: int, reason: str) -> None:
    """K6: remove our own IN_PROGRESS reservation after a pre-commit abort."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            deleted = await conn.fetchval(
                "DELETE FROM idempotency_keys WHERE key = $1 AND owner_receipt_id = $2 AND state = 'IN_PROGRESS' RETURNING key",
                key,
                receipt_id,
            )
            if deleted:
                await key_event(conn, key, "released", receipt_id, {"reason": reason})


async def recover_stuck_reservations(pool: asyncpg.Pool) -> int:
    """K7: at startup no request is in flight, so any IN_PROGRESS record did not commit (K1)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch("DELETE FROM idempotency_keys WHERE state = 'IN_PROGRESS' RETURNING key, owner_receipt_id")
            for row in rows:
                await key_event(conn, row["key"], "released_on_recovery", row["owner_receipt_id"])
    return len(rows)
