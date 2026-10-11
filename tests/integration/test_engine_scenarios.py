"""Engine + store + real provider: one deterministic scenario per controller path (DESIGN §2.5–§2.10a).

Each test asserts the safety property E ≤ 1 from the provider's oracle ledger, the claim, the rule
path, and a clean store audit. Expected outcomes are derived from the design, not from runs.
"""

from __future__ import annotations

import pytest

from raeg_gateway.controller import Policy
from raeg_gateway.transitions import State

from .conftest import assert_audit_clean, assert_decisions_recorded, scenario


def rule(idx: int, fault: str, **params) -> dict:
    return {"ingress_index": idx, "fault": fault, "params": params}


async def test_p1_no_fault(provider, store, engine, admin) -> None:
    o = await scenario(provider, store, engine, "P1", Policy())
    assert (o.state, o.effects, o.attempts) == (State.SUCCEEDED, 1, 1)
    assert o.rules == ["T1", "CLAIM-INITIAL", "D-COMMITTED"]
    await assert_audit_clean(admin)
    await assert_decisions_recorded(admin)


async def test_p1_lost_response_resolved_by_lookup_without_redispatch(provider, store, engine, admin) -> None:
    o = await scenario(provider, store, engine, "P1", Policy(), [rule(1, "LOST_RESPONSE")])
    assert (o.state, o.effects, o.attempts) == (State.SUCCEEDED, 1, 1)
    assert "D-AMBIGUOUS" in o.rules and o.rules[-1] == "L-S" and o.row["outcome_source"] == "lookup"
    await assert_audit_clean(admin)
    await assert_decisions_recorded(admin)


async def test_p4_stall_verified_absent_then_safe_retry(provider, store, engine, admin) -> None:
    o = await scenario(provider, store, engine, "P4", Policy(), [rule(1, "PRE_COMMIT_STALL", stall_ms=2500)])
    assert (o.state, o.effects, o.attempts) == (State.SUCCEEDED, 1, 2)
    assert "L-RA" in o.rules and o.row["segment_no"] == 2
    assert o.row["absence_proven_through"] is not None
    await assert_audit_clean(admin)


async def test_p4_delayed_commit_waits_for_horizon_and_finds_it(provider, store, engine, admin) -> None:
    """F4: commit after the client timeout but before d. An early miss must NOT be believed."""
    policy = Policy(attempt_ms=2000, margin_ms=800)  # client timeout ≈ 1.2 s; commit at ≈ 1.5 s < d = 2.0 s
    o = await scenario(provider, store, engine, "P4", policy, [rule(1, "DELAYED_COMMIT", delay_ms=1500)])
    assert (o.state, o.effects, o.attempts) == (State.SUCCEEDED, 1, 1)
    assert "L-W" in o.rules and o.rules[-1] == "L-S"
    await assert_audit_clean(admin)


async def test_p4_delayed_commit_no_horizon_ablation_duplicates(provider, store, engine, admin) -> None:
    """Ablation check (not a safety claim): believing an early miss re-dispatches and duplicates."""
    policy = Policy(attempt_ms=2000, margin_ms=800, no_horizon=True)
    o = await scenario(provider, store, engine, "P4", policy, [rule(1, "DELAYED_COMMIT", delay_ms=1500)])
    assert o.effects == 2 and o.attempts == 2, "the ablation removes the protection that makes F4 safe"
    await assert_audit_clean(admin)


async def test_p6_blind_provider_escalates_without_duplicate(provider, store, engine, admin) -> None:
    o = await scenario(provider, store, engine, "P6", Policy(), [rule(1, "LOST_RESPONSE")])
    assert (o.state, o.effects, o.attempts) == (State.ESCALATED, 1, 1)
    assert o.row["escalation_reason"] == "NO_SAFE_BASIS"
    await assert_audit_clean(admin)


async def test_p3_idempotent_replay_with_infinite_tau(provider, store, engine, admin) -> None:
    o = await scenario(provider, store, engine, "P3", Policy(), [rule(1, "LOST_RESPONSE")])
    assert (o.state, o.effects, o.attempts) == (State.SUCCEEDED, 1, 2)
    assert "R-RI" in o.rules and o.row["outcome_source"] == "replay"
    await assert_audit_clean(admin)


async def test_p7_replay_refused_when_retention_window_closed(provider, store, engine, admin) -> None:
    """Response withheld past the client timeout (≈1.4 s): d_new + ε + m ≈ 3.05 s > anchor + τ (3.0 s)
    → replay refused; no lookup → escalate. (A connection *reset* surfaces within ms instead: see below.)"""
    o = await scenario(provider, store, engine, "P7", Policy(),
                       [rule(1, "LOST_RESPONSE", mode="blackhole", blackhole_ms=3000)])
    assert (o.state, o.effects, o.attempts) == (State.ESCALATED, 1, 1)
    assert o.row["escalation_reason"] == "NO_SAFE_BASIS"
    await assert_audit_clean(admin)


async def test_p7_reset_after_commit_replays_within_retention(provider, store, engine, admin) -> None:
    """Reset right after commit is ambiguous within ms, so the retention window is still open: the
    replay returns the stored response (K3) and creates no second effect."""
    o = await scenario(provider, store, engine, "P7", Policy(), [rule(1, "LOST_RESPONSE", mode="reset")])
    assert (o.state, o.effects, o.attempts) == (State.SUCCEEDED, 1, 2)
    assert "R-RI" in o.rules and o.row["outcome_source"] == "replay"
    await assert_audit_clean(admin)


async def test_p7_fast_ambiguity_replays_within_retention(provider, store, engine, admin) -> None:
    o = await scenario(provider, store, engine, "P7", Policy(), [rule(1, "ERROR_5XX_PRE")])
    assert (o.state, o.effects, o.attempts) == (State.SUCCEEDED, 1, 2)
    assert "R-RI" in o.rules
    await assert_audit_clean(admin)


@pytest.mark.parametrize("mode,expected", [("STRICT", (State.ESCALATED, 0, 1)), ("BOUNDED", (State.SUCCEEDED, 1, 2))])
async def test_p4n_absence_needs_d_or_bounded_mode(provider, store, engine, admin, mode, expected) -> None:
    o = await scenario(provider, store, engine, "P4n", Policy(mode=mode), [rule(1, "PRE_COMMIT_STALL", stall_ms=2500)])
    assert (o.state, o.effects, o.attempts) == expected
    if mode == "STRICT":
        assert o.row["escalation_reason"] == "ABSENCE_UNPROVABLE"
    await assert_audit_clean(admin)


async def test_definitive_decline_on_single_attempt(provider, store, engine, admin) -> None:
    o = await scenario(provider, store, engine, "P4", Policy(), [rule(1, "FORCE_DECLINE")])
    assert (o.state, o.effects, o.attempts) == (State.DECLINED, 0, 1)
    assert o.rules[-1] == "D-DECLINED-DD"
    await assert_audit_clean(admin)


async def test_nodl_control_on_p4_cannot_prove_absence(provider, store, engine, admin) -> None:
    o = await scenario(provider, store, engine, "P4", Policy(send_deadline=False), [rule(1, "PRE_COMMIT_STALL", stall_ms=2500)])
    assert (o.state, o.effects) == (State.ESCALATED, 0)
    assert o.row["escalation_reason"] == "ABSENCE_UNPROVABLE"
    receipts = await provider.receipts(o.key)
    assert all(r["deadline"] is None for r in receipts), "no Request-Deadline header was sent"
    await assert_audit_clean(admin)


async def test_lookup_outage_exhausts_budget_and_escalates_safely(provider, store, engine, admin) -> None:
    rules = [rule(1, "PRE_COMMIT_STALL", stall_ms=2500)] + [
        {"kind": "lookup_ref", "ingress_index": i, "fault": "LOOKUP_UNAVAILABLE", "params": {}} for i in range(1, 10)
    ]
    o = await scenario(provider, store, engine, "P4", Policy(max_verify=3), rules)
    assert (o.state, o.effects, o.attempts) == (State.ESCALATED, 0, 1)
    assert o.row["escalation_reason"] in ("VERIFY_BUDGET_EXHAUSTED_BEFORE_HORIZON", "VERIFY_BUDGET_EXHAUSTED")
    assert o.row["verify_attempts"] == 3
    await assert_audit_clean(admin)
