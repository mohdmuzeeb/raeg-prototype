"""Audit of the gateway store: hash chains, projection rebuild (I8) and store-level invariants.

Used by tests now and by the post-run audit (audit.json) from M6 on. Every check reads the
database only; nothing here writes.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from raeg_gateway.store import GENESIS_HASH, event_body, event_hash, from_patch_json, to_patch_json
from raeg_gateway.transitions import ALL_COLUMNS, CLAIM_TRANSITIONS, LEASED, TERMINAL, State


def _json(value: Any) -> Any:
    """JSONB arrives as str on connections without a codec; the audit must not depend on that."""
    return json.loads(value) if isinstance(value, str) else value


async def verify_chains(conn: asyncpg.Connection) -> list[str]:
    """Recompute every event hash and link. Returns a list of problems (empty = intact)."""
    problems: list[str] = []
    rows = await conn.fetch("SELECT * FROM execution_events ORDER BY action_key, seq")
    prev_key, prev_hash, expected_seq = None, GENESIS_HASH, 1
    for r in rows:
        if r["action_key"] != prev_key:
            prev_key, prev_hash, expected_seq = r["action_key"], GENESIS_HASH, 1
        if r["seq"] != expected_seq:
            problems.append(f"{r['action_key']}: seq {r['seq']} where {expected_seq} expected")
        if r["prev_hash"] != prev_hash:
            problems.append(f"{r['action_key']} seq {r['seq']}: prev_hash does not link")
        body = event_body(
            r["action_key"], r["seq"], r["from_state"], r["to_state"], r["transition"], r["decision_id"],
            list(r["evidence_ids"]), _json(r["patch"]), r["worker_id"], r["at_db"],
        )
        if event_hash(r["prev_hash"], body) != r["hash"]:
            problems.append(f"{r['action_key']} seq {r['seq']}: hash mismatch (event content altered)")
        prev_hash, expected_seq = r["hash"], expected_seq + 1
    return problems


async def rebuild_projection(conn: asyncpg.Connection) -> dict[str, dict[str, Any]]:
    """Fold event patches in seq order. Seq 1 (T1) carries the complete initial row."""
    rebuilt: dict[str, dict[str, Any]] = {}
    for r in await conn.fetch("SELECT action_key, seq, patch FROM execution_events ORDER BY action_key, seq"):
        state = rebuilt.setdefault(r["action_key"], {})
        state.update(from_patch_json(_json(r["patch"])))
    return rebuilt


def _normalise(row: dict[str, Any]) -> dict[str, Any]:
    return to_patch_json({c: row.get(c) for c in ALL_COLUMNS})


async def compare_projection(conn: asyncpg.Connection) -> list[str]:
    """I8: the executions table equals the projection rebuilt from events, column by column."""
    problems: list[str] = []
    rebuilt = await rebuild_projection(conn)
    actual = {}
    for r in await conn.fetch("SELECT * FROM executions"):
        row = dict(r)
        for col in ("payload", "capability", "policy"):
            row[col] = _json(row[col])
        actual[r["action_key"]] = row
    for key in sorted(set(rebuilt) | set(actual)):
        if key not in actual or key not in rebuilt:
            problems.append(f"{key}: present only in {'events' if key in rebuilt else 'executions'}")
            continue
        a, b = _normalise(actual[key]), _normalise(rebuilt[key])
        for col in ALL_COLUMNS:
            if a[col] != b[col]:
                problems.append(f"{key}.{col}: table={a[col]!r} rebuilt={b[col]!r}")
    return problems


async def check_invariants(conn: asyncpg.Connection) -> dict[str, list[str]]:
    """Store-level invariants (DESIGN §3.3) that M3 can establish."""
    out: dict[str, list[str]] = {}

    # I2: leases exist exactly in leased states (also a table CHECK), and at most one per action by construction.
    rows = await conn.fetch("SELECT action_key, state, lease_owner, lease_expires_at FROM executions")
    out["I2_lease_consistency"] = [
        r["action_key"] for r in rows if (State(r["state"]) in LEASED) != (r["lease_owner"] is not None)
    ]

    # I5: nothing leaves an absorbing terminal state; ESCALATED leaves only via T16*.
    events = await conn.fetch("SELECT action_key, seq, from_state, to_state, transition, decision_id FROM execution_events")
    out["I5_absorbing"] = [
        f"{e['action_key']}#{e['seq']}"
        for e in events
        if e["from_state"] is not None
        and (
            State(e["from_state"]) in TERMINAL
            or (State(e["from_state"]) == State.ESCALATED and not e["transition"].startswith("T16"))
        )
    ]

    # I6: one applied decision per event, with the same transition id; no applied decision without an event.
    decisions = {
        d["id"]: d for d in await conn.fetch("SELECT id, action_key, transition, applied FROM decisions")
    }
    problems = []
    for e in events:
        d = decisions.get(e["decision_id"])
        if d is None or not d["applied"] or d["transition"] != e["transition"] or d["action_key"] != e["action_key"]:
            problems.append(f"{e['action_key']}#{e['seq']}: decision {e['decision_id']} missing/unapplied/mismatched")
    event_decisions = {e["decision_id"] for e in events}
    problems += [f"decision {i} applied but has no event" for i, d in decisions.items() if d["applied"] and i not in event_decisions]
    out["I6_decision_per_event"] = problems

    # Version = last event seq; event seqs contiguous (also checked by verify_chains).
    seqs = {r["action_key"]: r["m"] for r in await conn.fetch("SELECT action_key, max(seq) AS m FROM execution_events GROUP BY action_key")}
    out["version_equals_last_seq"] = [
        r["action_key"] for r in await conn.fetch("SELECT action_key, version FROM executions") if seqs.get(r["action_key"]) != r["version"]
    ]

    # Write-ahead bookkeeping: one attempts row per claim event, numbered 1..attempt_count.
    claims = {}
    for e in events:
        if e["transition"] in CLAIM_TRANSITIONS:
            claims[e["action_key"]] = claims.get(e["action_key"], 0) + 1
    attempt_rows = await conn.fetch("SELECT action_key, count(*) AS n, max(attempt_no) AS m FROM attempts GROUP BY action_key")
    counts = {r["action_key"]: r["attempt_count"] for r in await conn.fetch("SELECT action_key, attempt_count FROM executions")}
    problems = []
    for r in attempt_rows:
        if not (r["n"] == r["m"] == claims.get(r["action_key"], 0) == counts.get(r["action_key"])):
            problems.append(f"{r['action_key']}: attempts={r['n']} max={r['m']} claims={claims.get(r['action_key'])} count={counts.get(r['action_key'])}")
    problems += [k for k, n in claims.items() if k not in {r["action_key"] for r in attempt_rows}]
    out["attempts_match_claims"] = problems

    out["hash_chains"] = await verify_chains(conn)
    out["I8_projection_rebuild"] = await compare_projection(conn)
    return out
