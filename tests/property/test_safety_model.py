"""Model-based safety checks of the recovery policy (DESIGN §2.7, §3.3, §11).

Positive properties (must hold for every Hypothesis-generated adversary that respects the
assumption set):
  S      E(a) ≤ 1
  claims SUCCEEDED ⇒ E ≥ 1;  DECLINED ∨ NOT_EXECUTED ⇒ E = 0
  L      the action ends terminal or ESCALATED (no stuck state)
  anomalies: no contradiction anomalies when assumptions hold

Necessity checks: with one assumption deliberately broken, Hypothesis must FIND a violation.
These demonstrate that the corresponding assumption is load-bearing; they are not measurements.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.errors import Flaky

from raeg_common.models import Capability, LookupMode
from raeg_gateway.controller import Policy
from raeg_gateway.transitions import State

from .sim import Sim, Violations, caps

SETTINGS = settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
CONTRADICTIONS = {"CONTRADICTORY_EVIDENCE", "CONTRADICTS_TERMINAL_CLAIM", "DUPLICATE_DETECTED"}


def check_all(sim: Sim) -> None:
    sim.run()
    e = sim.effects
    state = sim.x["state"]
    assert e <= 1, f"SAFETY VIOLATION E={e}\n{sim.explain()}"
    if state == State.SUCCEEDED:
        assert e >= 1, f"false SUCCESS\n{sim.explain()}"
    if state in (State.DECLINED, State.NOT_EXECUTED):
        assert e == 0, f"false NO_EFFECT\n{sim.explain()}"
    assert state in (State.SUCCEEDED, State.DECLINED, State.NOT_EXECUTED, State.ESCALATED), f"stuck in {state}\n{sim.explain()}"
    assert not (set(sim.anomalies) & CONTRADICTIONS), f"anomaly under honoured assumptions\n{sim.explain()}"


@SETTINGS
@given(c=caps(), send_deadline=st.booleans(), order=st.sampled_from(["verify_first", "retry_first"]), data=st.data())
def test_tier_s_strict_is_safe_under_arbitrary_pauses_and_delays(c: Capability, send_deadline: bool, order: str, data) -> None:
    check_all(Sim(c, Policy(mode="STRICT", send_deadline=send_deadline, order=order), data))


@SETTINGS
@given(c=caps(), data=st.data())
def test_tier_b_bounded_is_safe_when_b1_holds(c: Capability, data) -> None:
    check_all(Sim(c, Policy(mode="BOUNDED"), data, Violations(b1=True)))


@SETTINGS
@given(c=caps().filter(lambda c: c.lookup == LookupMode.BY_CLIENT_REF and c.deadline_enforced), data=st.data())
def test_lookup_and_deadline_profiles_never_escalate_for_lack_of_basis(c: Capability, data) -> None:
    """With Q and D_eff, escalation can only come from exhausted budgets, never NO_SAFE_BASIS."""
    sim = Sim(c, Policy(mode="STRICT"), data)
    check_all(sim)
    if sim.x["state"] == State.ESCALATED:
        assert sim.x["escalation_reason"] != "NO_SAFE_BASIS", sim.explain()


# ----------------------------------------------------------------------------- necessity checks


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    return [exc]


def must_find_violation(test) -> None:
    """Run a Hypothesis test that asserts safety; it must fail with safety/claim assertion(s)."""
    try:
        test()
    except (AssertionError, Flaky, BaseExceptionGroup) as exc:
        leaves = _leaves(exc)
        assert all(isinstance(e, (AssertionError, Flaky)) for e in leaves), leaves
        messages = " | ".join(str(e).splitlines()[0] for e in leaves)
        assert "SAFETY VIOLATION" in messages or "false NO_EFFECT" in messages or "anomaly" in messages, messages
        print("counterexample(s):", messages)
        return
    raise AssertionError("no counterexample found: the assumption did not appear load-bearing")


FIND = settings(max_examples=3000, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
P4 = Capability(idempotency=False, lookup=LookupMode.BY_CLIENT_REF, lookup_lag_ms=500, deadline_enforced=True)
P4N = Capability(idempotency=False, lookup=LookupMode.BY_CLIENT_REF, lookup_lag_ms=500, deadline_enforced=False)
# Declared τ = 6000 so that replay after a timed-out attempt is permitted by the TTL guard; with
# τ = 3000 the guard already refuses every such replay and V3 is never exercised.
P2_LONG_TAU = Capability(idempotency=True, key_ttl_ms=6000, lookup=LookupMode.NONE, deadline_enforced=True)


def test_necessity_b1_bounded_mode_without_b1_can_duplicate() -> None:
    @FIND
    @given(data=st.data())
    def t(data):
        check_all(Sim(P4N, Policy(mode="BOUNDED"), data, Violations(b1=False)))
    must_find_violation(t)


def test_b1_as_worded_in_v03_is_insufficient_without_d() -> None:
    """Design finding (IMPLEMENTATION_NOTES N21): bounding only the *arrival* by d_j does not bound the
    effect when the provider has no D — slow processing can commit after d_j. B1 must bound the commit."""
    @FIND
    @given(data=st.data())
    def t(data):
        check_all(Sim(P4N, Policy(mode="BOUNDED"), data, Violations(b1=True, b1_arrival_only=True)))
    must_find_violation(t)


def test_necessity_horizon_no_horizon_ablation_can_duplicate() -> None:
    @FIND
    @given(data=st.data())
    def t(data):
        check_all(Sim(P4, Policy(mode="STRICT", no_horizon=True), data))
    must_find_violation(t)


def test_necessity_v1_lookup_lag_beyond_delta() -> None:
    @FIND
    @given(data=st.data())
    def t(data):
        check_all(Sim(P4, Policy(mode="STRICT"), data, Violations(lookup_lag_factor=8)))
    must_find_violation(t)


def test_necessity_v2_deadline_not_enforced() -> None:
    @FIND
    @given(data=st.data())
    def t(data):
        check_all(Sim(P4, Policy(mode="STRICT"), data, Violations(ignore_deadline=True)))
    must_find_violation(t)


def test_necessity_v3_retention_shorter_than_declared() -> None:
    @FIND
    @given(data=st.data())
    def t(data):
        check_all(Sim(P2_LONG_TAU, Policy(mode="STRICT"), data, Violations(retention_ms=500)))
    must_find_violation(t)


def test_necessity_strict_nodl_control_is_still_safe() -> None:
    """Control: the /noDL variant is *not* a violation — strict mode simply stops trusting absence."""
    @SETTINGS
    @given(data=st.data())
    def t(data):
        check_all(Sim(P4, Policy(mode="STRICT", send_deadline=False), data))
    t()
