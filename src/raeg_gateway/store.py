"""Persistent gateway store (DESIGN §3, §4, §5.2).

Every state change happens in one database transaction that
  1. locks the execution row (SELECT … FOR UPDATE),
  2. checks the transition table, the expected state/version and the lease fence,
  3. writes one `decisions` row (applied = true),
  4. updates the `executions` projection with an explicit patch,
  5. appends one hash-chained `execution_events` row carrying that exact patch.
A rejected request still writes a `decisions` row (applied = false) so that every decision is
auditable. The projection can be rebuilt from the events alone (invariant I8, see audit.py).

All times are the database clock: `now()` (transaction start) for state, which is never later
than any action taken after the transaction commits — so anchors and deadlines are conservative.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg

from raeg_common.fingerprint import canonicalize
from raeg_gateway.transitions import (
    ALL_COLUMNS,
    CLAIM_TRANSITIONS,
    LEASED,
    MUTABLE_COLUMNS,
    SAME_STATE_PATCH,
    TIMESTAMP_COLUMNS,
    Basis,
    IllegalTransition,
    State,
    check_transition,
)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
GENESIS_HASH = "0" * 64


# --------------------------------------------------------------------------- connection setup


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str, max_size: int = 40) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=max_size, init=_init_conn)


async def migrate(admin_dsn: str) -> list[str]:
    """Apply migrations as the owner role (not raeg_app)."""
    conn = await asyncpg.connect(admin_dsn)
    applied = []
    try:
        await conn.execute("SELECT pg_advisory_lock(515151)")
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
        await conn.execute("SELECT pg_advisory_unlock(515151)")
        await conn.close()
    return applied


# --------------------------------------------------------------------------- serialisation


def ts_str(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def epoch_us(value: datetime) -> int:
    return (value - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)


def to_patch_json(patch: dict[str, Any]) -> dict[str, Any]:
    """Typed patch -> JSON-safe patch (timestamps as UTC ISO strings with microseconds)."""
    out = {}
    for key, value in patch.items():
        if isinstance(value, datetime):
            out[key] = ts_str(value)
        elif isinstance(value, enum_types()):
            out[key] = value.value
        else:
            out[key] = value
    return out


def from_patch_json(patch: dict[str, Any]) -> dict[str, Any]:
    return {k: (datetime.fromisoformat(v) if k in TIMESTAMP_COLUMNS and v is not None else v) for k, v in patch.items()}


def enum_types() -> tuple[type, ...]:
    return (State, Basis)


def event_hash(prev_hash: str, body: dict[str, Any]) -> str:
    return hashlib.sha256(prev_hash.encode("ascii") + canonicalize(body)).hexdigest()


def event_body(
    action_key: str,
    seq: int,
    from_state: str | None,
    to_state: str,
    transition: str,
    decision_id: int,
    evidence_ids: list[int],
    patch_json: dict[str, Any],
    worker_id: str,
    at_db: datetime,
) -> dict[str, Any]:
    return {
        "action_key": action_key,
        "seq": seq,
        "from_state": from_state,
        "to_state": to_state,
        "transition": transition,
        "decision_id": decision_id,
        "evidence_ids": list(evidence_ids),
        "patch": patch_json,
        "worker_id": worker_id,
        "at_db_us": epoch_us(at_db),
    }


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class SubmitResult:
    outcome: str  # created | duplicate | fingerprint_conflict
    state: State
    version: int


@dataclass(frozen=True)
class TransitionResult:
    applied: bool
    reason: str  # "applied" | "STATE_MISMATCH" | "VERSION_MISMATCH" | "FENCED" | "LEASE_NOT_EXPIRED" | "NOT_DUE" | "GUARD_FAILED"
    decision_id: int
    version: int | None = None
    state: State | None = None


@dataclass(frozen=True)
class ClaimResult:
    applied: bool
    reason: str
    decision_id: int
    attempt_no: int | None = None
    deadline: datetime | None = None
    lease_token: datetime | None = None


@dataclass(frozen=True)
class OwnerFence:
    """The caller must hold the lease it was granted. `lease_token` is the granted lease_expires_at,
    unique per grant; `attempt_no` additionally pins a dispatch lease to its attempt."""

    worker_id: str
    lease_token: datetime
    attempt_no: int | None = None


@dataclass(frozen=True)
class ExpiredFence:
    """Sweeper: the lease must have expired on the database clock."""


Fence = OwnerFence | ExpiredFence

# guard(snapshot, now, new_deadline) -> (ok, rule_id, detail)
ClaimGuard = Callable[[dict[str, Any], datetime, datetime], tuple[bool, str, str]]


def always_ok(_snapshot: dict[str, Any], _now: datetime, _deadline: datetime) -> tuple[bool, str, str]:
    return True, "CLAIM", "unconditional"


# --------------------------------------------------------------------------- store


class GatewayStore:
    def __init__(self, pool: asyncpg.Pool, code_sha: str, policy_version: str) -> None:
        self.pool = pool
        self.code_sha = code_sha
        self.policy_version = policy_version

    # ------------------------------------------------------------------ T1 / T1a / T1b

    async def submit(
        self,
        action_key: str,
        payload: dict[str, Any],
        fingerprint: str,
        profile_id: str,
        capability: dict[str, Any],
        policy: dict[str, Any],
        worker_id: str,
    ) -> SubmitResult:
        async with self.pool.acquire() as conn, conn.transaction():
            now: datetime = await conn.fetchval("SELECT now()")
            row = {
                "action_key": action_key,
                "fingerprint": fingerprint,
                "payload": payload,
                "profile_id": profile_id,
                "capability": capability,
                "policy": policy,
                "created_at": now,
                "state": State.PREPARED,
                "version": 1,
                "updated_at": now,
                "attempt_count": 0,
                "verify_attempts": 0,
                "segment_no": 0,
                "anchor": None,
                "max_deadline": None,
                "absence_proven_through": None,
                "next_action_at": None,
                "lease_owner": None,
                "lease_expires_at": None,
                "retry_basis": None,
                "provider_txn_id": None,
                "outcome_source": None,
                "ambiguity_cause": None,
                "escalation_reason": None,
                "duplicate_detected": False,
                "pending_late_success": None,
            }
            assert set(row) == set(ALL_COLUMNS)
            columns = list(row)
            values = [row[c].value if isinstance(row[c], State) else row[c] for c in columns]
            placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
            inserted = await conn.fetchval(
                f"INSERT INTO executions ({', '.join(columns)}) VALUES ({placeholders}) "
                "ON CONFLICT (action_key) DO NOTHING RETURNING version",
                *values,
            )
            if inserted is None:
                existing = await conn.fetchrow(
                    "SELECT fingerprint, state, version FROM executions WHERE action_key = $1", action_key
                )
                outcome = "duplicate" if existing["fingerprint"] == fingerprint else "fingerprint_conflict"
                await self._submission(conn, action_key, fingerprint, payload, outcome, now)
                return SubmitResult(outcome, State(existing["state"]), existing["version"])

            await self._submission(conn, action_key, fingerprint, payload, "created", now)
            decision_id = await self._decision(
                conn, action_key, now, "T1", "T1", {"fingerprint": fingerprint, "profile_id": profile_id}, [], {"state": "PREPARED"}, True, worker_id
            )
            await self._event(conn, action_key, 1, None, State.PREPARED, "T1", decision_id, [], to_patch_json(row), worker_id, now)
            return SubmitResult("created", State.PREPARED, 1)

    # ------------------------------------------------------------------ generic transition

    async def transition(
        self,
        action_key: str,
        tid: str,
        *,
        worker_id: str,
        rule_id: str,
        inputs: dict[str, Any] | None = None,
        patch: dict[str, Any] | None = None,
        evidence_ids: list[int] | None = None,
        expected_version: int | None = None,
        fence: Fence | None = None,
        lease_ms: int | None = None,
    ) -> TransitionResult:
        if tid in CLAIM_TRANSITIONS:
            raise IllegalTransition(f"{tid} must go through claim()")
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM executions WHERE action_key = $1 FOR UPDATE", action_key)
            if row is None:
                raise KeyError(action_key)
            now: datetime = await conn.fetchval("SELECT now()")
            return await self._apply(
                conn, dict(row), tid, now=now, worker_id=worker_id, rule_id=rule_id, inputs=inputs or {},
                patch=patch or {}, evidence_ids=evidence_ids or [], expected_version=expected_version,
                fence=fence, lease_ms=lease_ms,
            )

    async def _apply(
        self,
        conn: asyncpg.Connection,
        row: dict[str, Any],
        tid: str,
        *,
        now: datetime,
        worker_id: str,
        rule_id: str,
        inputs: dict[str, Any],
        patch: dict[str, Any],
        evidence_ids: list[int],
        expected_version: int | None,
        fence: Fence | None,
        lease_ms: int | None,
    ) -> TransitionResult:
        current = State(row["state"])
        if isinstance(fence, OwnerFence) and (current not in LEASED or row["lease_owner"] != fence.worker_id):
            # A (possibly zombie) owner whose lease was swept or re-granted: a race, not a bug.
            did = await self._decision(conn, row["action_key"], now, rule_id, tid,
                                       {"state": current.value, "version": row["version"], **inputs},
                                       evidence_ids, {"rejected": "FENCED"}, False, worker_id)
            return TransitionResult(False, "FENCED", did, row["version"], current)
        if expected_version is not None and row["version"] != expected_version:
            # CAS caller lost a race: the row moved on since its snapshot. Refuse and record, before
            # judging legality against a state the caller never saw.
            did = await self._decision(conn, row["action_key"], now, rule_id, tid,
                                       {"state": current.value, "version": row["version"],
                                        "expected_version": expected_version, **inputs},
                                       evidence_ids, {"rejected": "VERSION_MISMATCH"}, False, worker_id)
            return TransitionResult(False, "VERSION_MISMATCH", did, row["version"], current)
        transition = check_transition(tid, current)  # raises IllegalTransition: a programming error
        if tid in SAME_STATE_PATCH and not set(patch) <= SAME_STATE_PATCH[tid]:
            raise IllegalTransition(f"{tid} may only set {sorted(SAME_STATE_PATCH[tid])}")
        bad = set(patch) - MUTABLE_COLUMNS - {"lease_owner", "lease_expires_at"}
        if bad or {"lease_owner", "lease_expires_at"} & set(patch):
            raise IllegalTransition(f"patch may not set {sorted(bad | ({'lease_owner', 'lease_expires_at'} & set(patch)))}")

        snapshot_in = {"state": current.value, "version": row["version"], "lease_owner": row["lease_owner"],
                       "lease_expires_at": ts_str(row["lease_expires_at"]) if row["lease_expires_at"] else None,
                       "attempt_count": row["attempt_count"], **inputs}

        async def reject(reason: str) -> TransitionResult:
            did = await self._decision(conn, row["action_key"], now, rule_id, tid, snapshot_in, evidence_ids,
                                       {"rejected": reason}, False, worker_id)
            return TransitionResult(False, reason, did, row["version"], current)

        if expected_version is not None and row["version"] != expected_version:
            return await reject("VERSION_MISMATCH")
        if current in LEASED:
            if fence is None:
                raise IllegalTransition(f"{tid} leaves a leased state and needs a fence")
            if isinstance(fence, OwnerFence):
                if (
                    row["lease_owner"] != fence.worker_id
                    or row["lease_expires_at"] != fence.lease_token
                    or (fence.attempt_no is not None and row["attempt_count"] != fence.attempt_no)
                ):
                    return await reject("FENCED")
            elif not (row["lease_expires_at"] < now):
                return await reject("LEASE_NOT_EXPIRED")

        full_patch: dict[str, Any] = dict(patch)
        if transition.target in LEASED:  # entering a leased state (T6); claims use claim()
            if lease_ms is None:
                raise IllegalTransition(f"{tid} grants a lease and needs lease_ms")
            full_patch["lease_owner"] = worker_id
            full_patch["lease_expires_at"] = now + timedelta(milliseconds=lease_ms)
        elif current in LEASED:
            full_patch["lease_owner"] = None
            full_patch["lease_expires_at"] = None
        return await self._commit_change(conn, row, current, transition.target, tid, full_patch, now, worker_id,
                                         rule_id, snapshot_in, evidence_ids)

    async def _commit_change(
        self,
        conn: asyncpg.Connection,
        row: dict[str, Any],
        current: State,
        target: State,
        tid: str,
        patch: dict[str, Any],
        now: datetime,
        worker_id: str,
        rule_id: str,
        inputs: dict[str, Any],
        evidence_ids: list[int],
    ) -> TransitionResult:
        version = row["version"] + 1
        full = {**patch, "state": target, "version": version, "updated_at": now}
        patch_json = to_patch_json(full)
        decision_id = await self._decision(conn, row["action_key"], now, rule_id, tid, inputs, evidence_ids,
                                           {"to_state": target.value, "patch": patch_json}, True, worker_id)
        columns = list(full)
        assignments = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(columns))
        values = [full[c].value if isinstance(full[c], (State, Basis)) else full[c] for c in columns]
        await conn.execute(f"UPDATE executions SET {assignments} WHERE action_key = $1", row["action_key"], *values)
        await self._event(conn, row["action_key"], version, current, target, tid, decision_id, evidence_ids,
                          patch_json, worker_id, now)
        return TransitionResult(True, "applied", decision_id, version, target)

    # ------------------------------------------------------------------ claims (T2 / T13 / T13a)

    async def claim(
        self,
        action_key: str,
        *,
        worker_id: str,
        attempt_ms: int,
        basis: Basis,
        guard: ClaimGuard = always_ok,
        expected_version: int | None = None,
    ) -> ClaimResult:
        """Write-ahead claim of the next attempt. The deadline d_n = now() + attempt_ms is persisted,
        together with the attempts row, before any request may be sent; the lease expires at d_n."""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM executions WHERE action_key = $1 FOR UPDATE", action_key)
            if row is None:
                raise KeyError(action_key)
            row = dict(row)
            now: datetime = await conn.fetchval("SELECT now()")
            current = State(row["state"])
            tid = "T2" if current == State.PREPARED else "T13"
            if current not in (State.PREPARED, State.RETRY_WAIT):
                did = await self._decision(conn, action_key, now, "CLAIM", tid, {"state": current.value}, [],
                                           {"rejected": "STATE_MISMATCH"}, False, worker_id)
                return ClaimResult(False, "STATE_MISMATCH", did)
            if (current == State.PREPARED) != (basis == Basis.INITIAL):
                raise IllegalTransition(f"basis {basis.value} invalid from {current.value}")
            if current == State.RETRY_WAIT and row["retry_basis"] != basis.value:
                raise IllegalTransition(f"claim basis {basis.value} != approved {row['retry_basis']}")
            inputs = {"state": current.value, "version": row["version"], "basis": basis.value,
                      "attempt_count": row["attempt_count"]}
            if expected_version is not None and row["version"] != expected_version:
                did = await self._decision(conn, action_key, now, "CLAIM", tid, inputs, [],
                                           {"rejected": "VERSION_MISMATCH"}, False, worker_id)
                return ClaimResult(False, "VERSION_MISMATCH", did)
            if row["next_action_at"] is not None and row["next_action_at"] > now:
                did = await self._decision(conn, action_key, now, "CLAIM", tid, inputs, [],
                                           {"rejected": "NOT_DUE"}, False, worker_id)
                return ClaimResult(False, "NOT_DUE", did)

            deadline = now + timedelta(milliseconds=attempt_ms)
            ok, rule_id, detail = guard(row, now, deadline)
            inputs = {**inputs, "new_deadline": ts_str(deadline), "guard_rule": rule_id, "guard_detail": detail}
            if not ok:
                if current == State.PREPARED:
                    raise IllegalTransition("the first attempt has no guard")
                result = await self._commit_change(
                    conn, row, current, State.UNKNOWN, "T13a",
                    {"ambiguity_cause": f"RETRY_GUARD_FAILED:{rule_id}", "next_action_at": None},
                    now, worker_id, rule_id, inputs, [],
                )
                return ClaimResult(False, "GUARD_FAILED", result.decision_id)

            attempt_no = row["attempt_count"] + 1
            new_segment = basis.starts_segment
            segment_no = row["segment_no"] + 1 if new_segment else row["segment_no"]
            patch = {
                "attempt_count": attempt_no,
                "segment_no": segment_no,
                "anchor": now if new_segment else row["anchor"],
                "max_deadline": max(d for d in (row["max_deadline"], deadline) if d is not None),
                "retry_basis": basis.value,
                "next_action_at": None,
                "lease_owner": worker_id,
                "lease_expires_at": deadline,
            }
            await conn.execute(
                """INSERT INTO attempts (action_key, attempt_no, deadline, segment_no, basis, worker_id, claimed_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                action_key, attempt_no, deadline, segment_no, basis.value, worker_id, now,
            )
            result = await self._commit_change(conn, row, current, State.DISPATCHING, tid, patch, now, worker_id,
                                               rule_id, inputs, [])
            return ClaimResult(True, "applied", result.decision_id, attempt_no, deadline, deadline)

    # ------------------------------------------------------------------ sweeper (T5 / T11 on lease expiry)

    async def sweep_expired(self, worker_id: str, limit: int = 100, system_id: str | None = None) -> list[TransitionResult]:
        results = []
        async with self.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """SELECT * FROM executions
                   WHERE state IN ('DISPATCHING', 'VERIFYING') AND lease_expires_at < now()
                     AND ($2::text IS NULL OR policy->>'system_id' = $2)
                   ORDER BY lease_expires_at LIMIT $1 FOR UPDATE SKIP LOCKED""",
                limit, system_id,
            )
            now: datetime = await conn.fetchval("SELECT now()")
            for row in rows:
                tid = "T5" if row["state"] == State.DISPATCHING.value else "T11"
                results.append(
                    await self._apply(
                        conn, dict(row), tid, now=now, worker_id=worker_id, rule_id="SWEEP_LEASE_EXPIRED",
                        inputs={}, patch={"ambiguity_cause": "LEASE_EXPIRED"}, evidence_ids=[],
                        expected_version=row["version"], fence=ExpiredFence(), lease_ms=None,
                    )
                )
        return results

    # ------------------------------------------------------------------ evidence and anomalies

    async def append_evidence(
        self,
        action_key: str,
        *,
        kind: str,
        worker_id: str,
        attempt_no: int | None = None,
        sent_at: datetime | None = None,
        provider_as_of: datetime | None = None,
        http_status: int | None = None,
        request: Any = None,
        response_headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
        classification: str | None = None,
        fenced: bool = False,
    ) -> int:
        async with self.pool.acquire() as conn:
            received: datetime = await conn.fetchval("SELECT clock_timestamp()")
            content = {
                "action_key": action_key,
                "attempt_no": attempt_no,
                "kind": kind,
                "sent_at": ts_str(sent_at) if sent_at else None,
                "received_at_db": ts_str(received),
                "provider_as_of": ts_str(provider_as_of) if provider_as_of else None,
                "http_status": http_status,
                "request": request,
                "response_headers": response_headers,
                "raw_body_b64": base64.b64encode(raw_body).decode() if raw_body is not None else None,
                "classification": classification,
                "worker_id": worker_id,
                "fenced": fenced,
            }
            digest = hashlib.sha256(canonicalize(content)).hexdigest()
            return await conn.fetchval(
                """INSERT INTO evidence (action_key, attempt_no, kind, sent_at, received_at_db, provider_as_of,
                                         http_status, request, response_headers, raw_body, classification,
                                         worker_id, fenced, sha256)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) RETURNING id""",
                action_key, attempt_no, kind, sent_at, received, provider_as_of, http_status, request,
                response_headers, raw_body, classification, worker_id, fenced, digest,
            )

    async def append_anomaly(self, action_key: str, kind: str, evidence_ids: list[int], detail: dict | None = None) -> int:
        return await self.pool.fetchval(
            "INSERT INTO anomalies (action_key, kind, evidence_ids, detail, at_db) VALUES ($1, $2, $3, $4, clock_timestamp()) RETURNING id",
            action_key, kind, evidence_ids, detail,
        )

    async def note_decision(
        self, action_key: str, *, worker_id: str, rule_id: str, inputs: dict[str, Any], evidence_ids: list[int],
        output: dict[str, Any],
    ) -> int:
        """A decision that changes no state (e.g. late evidence consumed, audit found nothing).
        Recorded with applied = false so I6 (one applied decision per event) is unaffected."""
        async with self.pool.acquire() as conn:
            now: datetime = await conn.fetchval("SELECT now()")
            return await self._decision(conn, action_key, now, rule_id, None, inputs, evidence_ids, output, False, worker_id)

    async def now(self) -> datetime:
        return await self.pool.fetchval("SELECT now()")

    async def attempts(self, action_key: str) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.pool.fetch(
            "SELECT * FROM attempts WHERE action_key = $1 ORDER BY attempt_no", action_key)]

    async def due_actions(self, limit: int = 100, system_id: str | None = None) -> list[str]:
        """Actions the controller should look at: lease-free, non-terminal, and due."""
        rows = await self.pool.fetch(
            """SELECT action_key FROM executions
               WHERE state IN ('PREPARED', 'UNKNOWN', 'RETRY_WAIT')
                 AND (next_action_at IS NULL OR next_action_at <= now())
                 AND ($2::text IS NULL OR policy->>'system_id' = $2)
               ORDER BY coalesce(next_action_at, updated_at) LIMIT $1""",
            limit, system_id,
        )
        return [r["action_key"] for r in rows]

    async def audit_candidates(self, limit: int = 100, system_id: str | None = None) -> list[str]:
        """SUCCEEDED actions with >1 attempt and no duplicate audit yet (rule DS; the engine filters further)."""
        rows = await self.pool.fetch(
            """SELECT x.action_key FROM executions x
               WHERE x.state = 'SUCCEEDED' AND NOT x.duplicate_detected AND x.attempt_count > 1
                 AND NOT EXISTS (SELECT 1 FROM evidence e WHERE e.action_key = x.action_key AND e.kind = 'duplicate_audit')
                 AND ($2::text IS NULL OR x.policy->>'system_id' = $2)
               ORDER BY x.updated_at LIMIT $1""",
            limit, system_id,
        )
        return [r["action_key"] for r in rows]

    async def unconsumed_late_evidence(self, limit: int = 100, system_id: str | None = None) -> list[dict[str, Any]]:
        """Late responses not yet cited by any decision (applied or not)."""
        rows = await self.pool.fetch(
            """SELECT e.* FROM evidence e JOIN executions x USING (action_key)
               WHERE e.kind = 'late_response'
                 AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.action_key = e.action_key AND e.id = ANY (d.evidence_ids))
                 AND ($2::text IS NULL OR x.policy->>'system_id' = $2)
               ORDER BY e.id LIMIT $1""",
            limit, system_id,
        )
        return [dict(r) for r in rows]

    async def evidence_rows(self, action_key: str, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is None:
            rows = await self.pool.fetch("SELECT * FROM evidence WHERE action_key = $1 ORDER BY id", action_key)
        else:
            rows = await self.pool.fetch(
                "SELECT * FROM evidence WHERE action_key = $1 AND kind = $2 ORDER BY id", action_key, kind)
        return [dict(r) for r in rows]

    async def snapshot(self, action_key: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow("SELECT * FROM executions WHERE action_key = $1", action_key)
        return dict(row) if row else None

    # ------------------------------------------------------------------ internals

    async def _submission(self, conn, action_key, fingerprint, payload, outcome, now) -> None:
        await conn.execute(
            "INSERT INTO submissions (action_key, fingerprint, payload, outcome, at_db) VALUES ($1, $2, $3, $4, clock_timestamp())",
            action_key, fingerprint, payload, outcome,
        )

    async def _decision(self, conn, action_key, now, rule_id, tid, inputs, evidence_ids, output, applied, worker_id) -> int:
        return await conn.fetchval(
            """INSERT INTO decisions (action_key, at_db, rule_id, transition, policy_version, code_sha, inputs,
                                      evidence_ids, output, applied, worker_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING id""",
            action_key, now, rule_id, tid, self.policy_version, self.code_sha,
            json.loads(json.dumps(inputs, default=_json_default)), evidence_ids,
            json.loads(json.dumps(output, default=_json_default)), applied, worker_id,
        )

    async def _event(self, conn, action_key, seq, from_state, to_state, tid, decision_id, evidence_ids, patch_json, worker_id, now) -> None:
        if seq == 1:
            prev = GENESIS_HASH
        else:
            prev = await conn.fetchval(
                "SELECT hash FROM execution_events WHERE action_key = $1 AND seq = $2", action_key, seq - 1
            )
            if prev is None:
                raise RuntimeError(f"event chain broken for {action_key}: seq {seq - 1} missing")
        body = event_body(action_key, seq, from_state.value if from_state else None, to_state.value, tid,
                          decision_id, evidence_ids, patch_json, worker_id, now)
        await conn.execute(
            """INSERT INTO execution_events (action_key, seq, from_state, to_state, transition, decision_id,
                                             evidence_ids, patch, worker_id, at_db, prev_hash, hash)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)""",
            action_key, seq, body["from_state"], body["to_state"], tid, decision_id, list(evidence_ids),
            patch_json, worker_id, now, prev, event_hash(prev, body),
        )


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return ts_str(value)
    if isinstance(value, (State, Basis)):
        return value.value
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")
