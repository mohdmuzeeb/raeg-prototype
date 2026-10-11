"""Exhaustive decision-table tests of the pure controller (DESIGN v0.3 §2.5–§2.10a).

Every combination of capability × policy × budget × timing is enumerated. Each result is checked
against predicates written directly from the DESIGN text (not by calling controller helpers),
plus hand-derived golden rows. Nothing here touches a database or network.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from raeg_common.fingerprint import fingerprint
from raeg_common.models import Capability, LookupMode, Observation
from raeg_gateway import controller as ctl
from raeg_gateway.controller import AttemptView, LookupView, Policy, Snapshot
from raeg_gateway.transitions import Basis, State

T0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)
DELTA = 500
TAU = 3000
PAYLOAD = {"amount_minor": 1000, "currency": "USD", "payer_id": "p", "payee_id": "m", "memo": ""}
FP = fingerprint(PAYLOAD)


def ms(n: float) -> timedelta:
    return timedelta(milliseconds=n)


def cap(i: bool, tau: int | None, q: LookupMode, d: bool) -> Capability:
    return Capability(idempotency=i, key_ttl_ms=tau if i else None, lookup=q,
                      lookup_lag_ms=DELTA if q != LookupMode.NONE else None, deadline_enforced=d)


def snap(state: State, *, attempts: int = 1, verify: int = 0, anchor: datetime = T0, max_deadline: datetime | None = None,
         seg_layout: list[int] | None = None, apt: datetime | None = None, esc: str | None = None,
         deadlines: list[datetime] | None = None, txn: str | None = None) -> Snapshot:
    seg_layout = seg_layout or [1] * attempts
    deadlines = deadlines or [anchor + ms(1500 * (i + 1)) for i in range(attempts)]
    md = max_deadline or max(deadlines)
    return Snapshot("k1", FP, state, attempts, verify, seg_layout[-1], anchor, md, apt, None, txn, esc,
                    tuple(AttemptView(i + 1, deadlines[i], seg_layout[i], "INITIAL") for i in range(attempts)))


CAPS = [cap(i, tau, q, d) for i in (True, False) for tau in ((None, TAU) if i else (None,))
        for q in LookupMode for d in (True, False)]
POLICIES = [Policy(mode=m, send_deadline=sd, order=o) for m in ("STRICT", "BOUNDED") for sd in (True, False)
            for o in ("verify_first", "retry_first")]
RULES = {"R-E-PENDING", "R-RI", "R-V-VB1-WAIT", "R-E-VB2", "R-V", "R-E-NO_SAFE_BASIS", "R-E-VERIFY_BUDGET_EXHAUSTED",
         "R-E-VERIFY_BUDGET_EXHAUSTED_BEFORE_HORIZON", "R-E-DISPATCH_BUDGET_EXHAUSTED"}


def design_replay_ok(c: Capability, p: Policy, anchor: datetime, now: datetime) -> bool:
    """§2.6 written out: I ∧ (τ = ∞ ∨ ((D ∧ header) ∨ BOUNDED) ∧ (now + T_att) + ε + m ≤ anchor + τ)."""
    if not c.idempotency:
        return False
    if c.key_ttl_ms is None:
        return True
    enforced = (c.deadline_enforced and p.send_deadline) or p.mode == "BOUNDED"
    return enforced and now + ms(p.attempt_ms + p.clock_skew_ms + p.margin_ms) <= anchor + ms(c.key_ttl_ms)


# ----------------------------------------------------------------------------- decide()

DECIDE_CASES = list(itertools.product(
    range(len(CAPS)), range(len(POLICIES)),
    (1, 2, 3),                      # attempt_count (max_dispatch = 3)
    (0, 3, 4),                      # verify_attempts (max_verify = 4)
    (-20_000, -200, 200),           # now relative to settle = max_deadline + δ (ms)
    (500, 2000),                    # now - anchor (ms): replay window open / closed for τ = 3000
    (None, "ABSENCE_UNPROVABLE"),   # pending escalation
))


def test_decide_case_count() -> None:
    assert len(DECIDE_CASES) == len(CAPS) * len(POLICIES) * 3 * 3 * 3 * 2 * 2 == 15552


def run_table(cases, check) -> None:
    """Run every case; report all failing combinations at once (not just the first)."""
    failures = []
    for case in cases:
        try:
            check(*case)
        except AssertionError as exc:
            failures.append(f"{case}: {exc}")
    assert not failures, f"{len(failures)} failing cases, first 10:\n" + "\n".join(failures[:10])


@pytest.mark.parametrize("ci", range(len(CAPS)), ids=lambda i: f"cap{i}")
def test_decide_table(ci: int) -> None:
    cases = [c for c in DECIDE_CASES if c[0] == ci]
    assert len(cases) == len(DECIDE_CASES) // len(CAPS) == 864
    run_table(cases, check_decide)


def check_decide(ci: int, pi: int, att: int, ver: int, rel: int, age: int, esc: str | None) -> None:
    c, p = CAPS[ci], POLICIES[pi]
    md = T0 + ms(10_000)
    now = md + ms(DELTA) + ms(rel)
    anchor = now - ms(age)
    deadlines = [md - ms(100 * (att - 1 - i)) for i in range(att)]
    x = snap(State.UNKNOWN, attempts=att, verify=ver, anchor=anchor, max_deadline=md, deadlines=deadlines, esc=esc)
    d = ctl.decide(x, c, p, now)
    assert d.rule_id in RULES
    assert d.basis in (None, Basis.IDEMPOTENT_REPLAY), "decide() never grants VERIFIED_ABSENT (only on_lookup)"

    if esc:
        assert (d.transition, d.patch) == ("T14", {"escalation_reason": esc})
        return
    lookup = c.lookup == LookupMode.BY_CLIENT_REF
    replay = att < 3 and design_replay_ok(c, p, anchor, now)
    settle = md + ms(DELTA)
    if d.transition == "T12":
        assert replay, "R-RI only under the §2.6 guard and dispatch budget"
    if p.order == "retry_first" and replay:
        assert d.transition == "T12"
        return
    if lookup and ver < 4:
        if now < settle and ver >= 3:  # VB1: last unit reserved for t ≥ H + δ
            expected = "T14" if settle - now > ms(p.max_settle_wait_ms) else "TW"
            assert d.transition == expected
            if expected == "TW":
                assert d.patch == {"next_action_at": settle}
        else:
            assert (d.transition, d.patch) == ("T6", {"verify_attempts": ver + 1})
        return
    if replay:
        assert d.transition == "T12"
        return
    assert d.transition == "T14"
    reason = d.patch["escalation_reason"]
    if lookup and ver >= 4:
        assert reason in ("VERIFY_BUDGET_EXHAUSTED", "VERIFY_BUDGET_EXHAUSTED_BEFORE_HORIZON")
    elif att >= 3:
        assert reason == "DISPATCH_BUDGET_EXHAUSTED"
    else:
        assert reason == "NO_SAFE_BASIS"


def test_strict_without_deadline_never_replays_finite_tau() -> None:
    """Tier S: with finite τ and no enforced deadline, a zombie could arrive after expiry (§2.7)."""
    for q, sd in itertools.product(LookupMode, (True, False)):
        c = cap(True, TAU, q, d=False)
        p = Policy(mode="STRICT", send_deadline=sd, order="retry_first")
        x = snap(State.UNKNOWN, anchor=T0, max_deadline=T0 + ms(1500))
        assert ctl.decide(x, c, p, T0 + ms(1)).transition != "T12"


def test_nodl_control_disables_deadline_reasoning() -> None:
    c = cap(True, TAU, LookupMode.NONE, d=True)
    x = snap(State.UNKNOWN, anchor=T0, max_deadline=T0 + ms(1500))
    assert ctl.decide(x, c, Policy(send_deadline=True), T0 + ms(1)).transition == "T12"
    assert ctl.decide(x, c, Policy(send_deadline=False), T0 + ms(1)).transition == "T14"


# ----------------------------------------------------------------------------- golden rows (hand-derived)

GOLDEN = [
    # (description, cap, policy, attempts, verify, now-offset-from-settle ms, anchor-age ms, expected rule)
    ("P1 fresh ambiguity: verify first", cap(True, None, LookupMode.BY_CLIENT_REF, True), Policy(), 1, 0, -1000, 500, "R-V"),
    ("P3 (I, τ=∞, no Q, no D): replay is Tier S safe", cap(True, None, LookupMode.NONE, False), Policy(), 1, 0, -1000, 500, "R-RI"),
    ("P6 blind: escalate", cap(False, None, LookupMode.NONE, False), Policy(), 1, 0, -1000, 500, "R-E-NO_SAFE_BASIS"),
    ("P7 within retention: replay", cap(True, TAU, LookupMode.NONE, True), Policy(), 1, 0, -1000, 500, "R-RI"),
    ("P7 retention window closed: escalate", cap(True, TAU, LookupMode.NONE, True), Policy(), 1, 0, -1000, 2000, "R-E-NO_SAFE_BASIS"),
    ("P4 last verify reserved before horizon: wait", cap(False, None, LookupMode.BY_CLIENT_REF, True), Policy(), 1, 3, -200, 500, "R-V-VB1-WAIT"),
    ("P4 last verify after horizon: verify", cap(False, None, LookupMode.BY_CLIENT_REF, True), Policy(), 1, 3, 200, 500, "R-V"),
    ("P4 budget exhausted: escalate (VB4, no replay)", cap(False, None, LookupMode.BY_CLIENT_REF, True), Policy(), 1, 4, 200, 500,
     "R-E-VERIFY_BUDGET_EXHAUSTED_BEFORE_HORIZON"),
    ("P2 budget exhausted but replay window open: replay only (VB4)", cap(True, TAU, LookupMode.BY_CLIENT_REF, True), Policy(), 1, 4, 200, 500, "R-RI"),
    ("P5 lookup by id only: treated as no lookup", cap(False, None, LookupMode.BY_PROVIDER_ID, False), Policy(), 1, 0, -1000, 500, "R-E-NO_SAFE_BASIS"),
    ("no-verify ablation on P1: replay", cap(True, None, LookupMode.BY_CLIENT_REF, True), Policy(no_verify=True), 1, 0, -1000, 500, "R-RI"),
    ("dispatch budget exhausted on P3", cap(True, None, LookupMode.NONE, False), Policy(), 3, 0, -1000, 500, "R-E-DISPATCH_BUDGET_EXHAUSTED"),
]


@pytest.mark.parametrize("desc,c,p,att,ver,rel,age,expected", GOLDEN, ids=[g[0] for g in GOLDEN])
def test_golden_decide(desc, c, p, att, ver, rel, age, expected) -> None:
    md = T0 + ms(10_000)
    now = md + ms(DELTA) + ms(rel)
    x = snap(State.UNKNOWN, attempts=att, verify=ver, anchor=now - ms(age), max_deadline=md)
    assert ctl.decide(x, c, p, now).rule_id == expected


def test_vb2_settle_wait_beyond_policy_escalates() -> None:
    c = cap(False, None, LookupMode.BY_CLIENT_REF, True)
    md = T0 + ms(60_000)
    x = snap(State.UNKNOWN, verify=3, max_deadline=md)
    d = ctl.decide(x, c, Policy(max_settle_wait_ms=10_000), T0)
    assert d.rule_id == "R-E-VB2" and d.patch == {"escalation_reason": "SETTLE_WAIT_EXCEEDS_POLICY"}


# ----------------------------------------------------------------------------- on_lookup()

REC_SETS = {
    "none": [],
    "declined": [{"client_reference": "k1", "status": "declined", "provider_txn_id": "d1"}],
    "succeeded": [{"client_reference": "k1", "status": "succeeded", "provider_txn_id": "s1"}],
    "succeeded2": [{"client_reference": "k1", "status": "succeeded", "provider_txn_id": "s1"},
                   {"client_reference": "k1", "status": "succeeded", "provider_txn_id": "s2"}],
    "mixed": [{"client_reference": "k1", "status": "declined", "provider_txn_id": "d1"},
              {"client_reference": "k1", "status": "succeeded", "provider_txn_id": "s1"}],
    "other_ref": [{"client_reference": "zzz", "status": "succeeded", "provider_txn_id": "x"}],
}
LOOKUP_CASES = list(itertools.product(
    [c for c in CAPS if c.lookup == LookupMode.BY_CLIENT_REF], range(len(POLICIES)), sorted(REC_SETS),
    (-200, 0, 200),       # as_of relative to settle (0 = exactly at H + δ)
    (1, 3),               # attempt_count
    ("single", "two_in_segment", "after_absence"),
    (True, False),        # lookup ok
))


LOOKUP_CAPS = [c for c in CAPS if c.lookup == LookupMode.BY_CLIENT_REF]


@pytest.mark.parametrize("ci", range(len(LOOKUP_CAPS)), ids=lambda i: f"lookupcap{i}")
def test_on_lookup_table(ci: int) -> None:
    cases = [case for case in LOOKUP_CASES if case[0] == LOOKUP_CAPS[ci]]
    assert len(cases) == len(POLICIES) * len(REC_SETS) * 3 * 2 * 3 * 2
    run_table(cases, check_on_lookup)


def check_on_lookup(c: Capability, pi: int, recs: str, rel: int, att: int, layout: str, ok: bool) -> None:
    p = POLICIES[pi]
    md = T0 + ms(10_000)
    settle = md + ms(DELTA)
    as_of = settle + ms(rel)
    now = as_of + ms(5)
    n = max(att, 2) if layout != "single" else att
    seg = {"single": [1] * n, "two_in_segment": [1] * n, "after_absence": [1] * (n - 1) + [2]}[layout]
    if layout == "single":
        seg = list(range(1, n + 1))  # every attempt in its own segment
    deadlines = [md - ms(100 * (n - 1 - i)) for i in range(n)]
    x = snap(State.VERIFYING, attempts=n, anchor=md - ms(1500), max_deadline=md, seg_layout=seg, deadlines=deadlines)
    x = Snapshot(**{**x.__dict__, "attempt_count": att if layout == "single" else n})
    r = LookupView(ok, as_of if ok else None, tuple(REC_SETS[recs]))
    d = ctl.on_lookup(x, r, c, p, now)

    mine = [r for r in REC_SETS[recs] if r["client_reference"] == "k1"]
    succ = [r for r in mine if r["status"] == "succeeded"]
    decl = [r for r in mine if r["status"] == "declined"]
    enforced = (c.deadline_enforced and p.send_deadline) or p.mode == "BOUNDED"
    authoritative = ok and enforced and as_of >= settle
    if not ok:
        assert d.rule_id == "L-ERR" and d.transition == "T11" and d.patch["next_action_at"] > now
        return
    if succ:
        assert d.transition == "T7" and d.patch["duplicate_detected"] == (len(succ) > 1)
        return
    if d.transition in ("T9", "T10"):
        assert authoritative and not decl, "NOT_EXECUTED / VERIFIED_ABSENT need authoritative absence (§2.5)"
        assert (d.transition == "T10") == (x.attempt_count < p.max_dispatch)
        assert d.patch["absence_proven_through"] == md
    if d.transition == "T8":
        assert decl and (authoritative or (not enforced and ctl.single_processing(x, c, p)[0]))
    if not authoritative:
        assert d.transition in ("T8", "T11")
        if enforced:
            assert d.rule_id in ("L-W", "L-W-VB2") and d.transition == "T11"
        elif d.transition == "T11":
            assert d.patch == {"escalation_reason": "ABSENCE_UNPROVABLE"}
    else:
        assert d.transition == ("T8" if decl else ("T10" if x.attempt_count < 3 else "T9"))


def test_as_of_exactly_at_settle_is_authoritative() -> None:
    c = cap(False, None, LookupMode.BY_CLIENT_REF, True)
    md = T0 + ms(10_000)
    x = snap(State.VERIFYING, max_deadline=md)
    assert ctl.on_lookup(x, LookupView(True, md + ms(DELTA)), c, Policy(), md + ms(DELTA)).rule_id == "L-RA"
    assert ctl.on_lookup(x, LookupView(True, md + ms(DELTA) - timedelta(microseconds=1)), c, Policy(),
                         md + ms(DELTA)).rule_id == "L-W"


def test_no_horizon_ablation_believes_any_miss() -> None:
    c = cap(False, None, LookupMode.BY_CLIENT_REF, True)
    x = snap(State.VERIFYING, max_deadline=T0 + ms(10_000))
    assert ctl.on_lookup(x, LookupView(True, T0), c, Policy(no_horizon=True), T0).rule_id == "L-RA"


# ----------------------------------------------------------------------------- replay guard boundary, DD, claim guard


def test_replay_guard_boundary_is_inclusive() -> None:
    c = cap(True, TAU, LookupMode.NONE, True)
    p = Policy()
    x = snap(State.UNKNOWN, anchor=T0)
    limit = T0 + ms(TAU) - ms(p.clock_skew_ms + p.margin_ms)
    assert ctl.replay_ok(x, c, p, limit) == (True, "WITHIN_RETENTION")
    assert ctl.replay_ok(x, c, p, limit + timedelta(microseconds=1)) == (False, "RETENTION_WINDOW_CLOSED")


@pytest.mark.parametrize(
    "c,layout,deadline_offsets,expected",
    [
        (cap(False, None, LookupMode.NONE, False), [1], [1500], (True, "SINGLE_ATTEMPT_IN_SEGMENT")),
        (cap(False, None, LookupMode.NONE, False), [1, 1], [1500, 3000], (False, "OTHER_ATTEMPTS_NOT_RULED_OUT")),
        (cap(False, None, LookupMode.NONE, False), [1, 2], [1500, 3000], (True, "SINGLE_ATTEMPT_IN_SEGMENT")),
        (cap(True, None, LookupMode.NONE, False), [1, 1, 1], [1500, 3000, 4500], (True, "KEY_NEVER_EXPIRES")),
        (cap(True, TAU, LookupMode.NONE, True), [1, 1], [1500, 2800], (True, "KEY_WITHIN_RETENTION_FOR_SEGMENT")),
        (cap(True, TAU, LookupMode.NONE, True), [1, 1], [1500, 2900], (False, "OTHER_ATTEMPTS_NOT_RULED_OUT")),
        (cap(True, TAU, LookupMode.NONE, False), [1, 1], [1500, 2000], (False, "OTHER_ATTEMPTS_NOT_RULED_OUT")),
    ],
)
def test_rule_dd(c, layout, deadline_offsets, expected) -> None:
    x = snap(State.DISPATCHING, attempts=len(layout), anchor=T0, seg_layout=layout,
             deadlines=[T0 + ms(o) for o in deadline_offsets])
    assert ctl.single_processing(x, c, Policy()) == expected


def test_decline_without_dd_is_ambiguous_not_terminal() -> None:
    """The 'insufficient funds because attempt 1 committed' case (§2.8)."""
    c = cap(False, None, LookupMode.BY_CLIENT_REF, True)
    two = snap(State.DISPATCHING, attempts=2, seg_layout=[1, 1])
    d = ctl.on_dispatch_result(two, Observation.DECLINED, c, Policy())
    assert (d.rule_id, d.transition) == ("D-DECLINED-NOT-DEFINITIVE", "T5")
    one = snap(State.DISPATCHING, attempts=1)
    assert ctl.on_dispatch_result(one, Observation.DECLINED, c, Policy()).transition == "T4"


@pytest.mark.parametrize(
    "obs,expected",
    [(Observation.COMMITTED, ("D-COMMITTED", "T3")), (Observation.DEADLINE_REJECTED, ("D-DEADLINE-REJECTED", "T5")),
     (Observation.AMBIGUOUS, ("D-AMBIGUOUS", "T5")), (Observation.INTERNAL, ("D-INTERNAL", "T5"))],
)
def test_dispatch_observations(obs, expected) -> None:
    d = ctl.on_dispatch_result(snap(State.DISPATCHING), obs, cap(True, None, LookupMode.NONE, True), Policy(),
                               provider_txn_id="t", error_class="ReadTimeout")
    assert (d.rule_id, d.transition) == expected
    if obs == Observation.INTERNAL:
        assert d.patch["escalation_reason"] == "INTERNAL"


def test_claim_guard_rechecks_against_new_deadline() -> None:
    c = cap(True, TAU, LookupMode.BY_CLIENT_REF, True)
    guard = ctl.claim_guard(c, Policy())
    row = {"action_key": "k1", "fingerprint": FP, "state": "RETRY_WAIT", "attempt_count": 1, "verify_attempts": 0,
           "segment_no": 1, "anchor": T0, "max_deadline": T0 + ms(1500), "absence_proven_through": None,
           "retry_basis": "IDEMPOTENT_REPLAY", "provider_txn_id": None, "escalation_reason": None}
    assert guard(row, T0, T0 + ms(2800))[0] is True
    assert guard(row, T0, T0 + ms(2900)) == (False, "G-RI", "RETENTION_WINDOW_CLOSED")
    va = {**row, "retry_basis": "VERIFIED_ABSENT", "absence_proven_through": T0 + ms(1500)}
    assert guard(va, T0, T0 + ms(99_999))[0] is True
    assert guard({**va, "max_deadline": T0 + ms(1600)}, T0, T0)[0] is False, "stale absence proof"
    assert guard({**row, "attempt_count": 3}, T0, T0)[:2] == (False, "G-BUDGET")


# ----------------------------------------------------------------------------- late success


def body(**over) -> dict:
    return {"client_reference": "k1", "status": "succeeded", "provider_txn_id": "s1", **PAYLOAD, **over}


@pytest.mark.parametrize(
    "attempt_no,b,failure",
    [(9, body(), "C-1"), (1, body(client_reference="x"), "C-2: client_reference"),
     (1, body(amount_minor=1), "C-2: echoed payload"), (1, body(status="declined"), "C-3"),
     (1, body(provider_txn_id=None), "C-3")],
)
def test_late_success_correlation_failures(attempt_no, b, failure) -> None:
    d = ctl.late_success(snap(State.UNKNOWN), attempt_no, b)
    assert d.rule_id == "LS-UNCORRELATED" and d.transition is None and failure in d.detail


def test_late_success_by_state() -> None:
    proof = T0 + ms(1500)
    cases = [
        (snap(State.UNKNOWN), ("LS-OK", "T15")),
        (snap(State.RETRY_WAIT), ("LS-OK", "T15")),
        (snap(State.UNKNOWN, apt=proof), ("LS-CONTRA", "T15a")),
        (snap(State.RETRY_WAIT, apt=proof), ("LS-CONTRA", "T15a")),
        (snap(State.SUCCEEDED, txn="s1"), ("LS-CONSISTENT", None)),
        (snap(State.SUCCEEDED, txn="other"), ("LS-DUPLICATE", "TA")),
        (snap(State.DECLINED), ("LS-CONTRADICTS-TERMINAL", None)),
        (snap(State.NOT_EXECUTED), ("LS-CONTRADICTS-TERMINAL", None)),
        (snap(State.ESCALATED), ("LS-FOR-ESCALATED", None)),
    ]
    for x, expected in cases:
        d = ctl.late_success(x, 1, body())
        assert (d.rule_id, d.transition) == expected, x.state
    for state in (State.PREPARED, State.DISPATCHING, State.VERIFYING):
        assert ctl.late_success(snap(state), 1, body()) is None, "deferred while leased"
