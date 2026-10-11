"""Capability-aware recovery policy R (DESIGN v0.3 §2.5–§2.10a). Pure functions: no I/O, `now` is an
argument, every result carries a rule id and the transition the engine must apply.

Section map:
  d_eff / horizon_enforced ........ §2.3 (D_eff), §2.7 (Tier S STRICT vs Tier B BOUNDED)
  absence_authoritative ........... §2.5
  replay_ok ....................... §2.6 TTL guard (segment anchor, deadline-based)
  single_processing (rule DD) ..... §2.8
  on_dispatch_result .............. §2.9 observations, DD/DS, T3/T4/T5
  decide .......................... §2.10 decide() + VB1/VB2/VB4, R-E* reasons
  on_lookup ....................... §2.10 on_lookup() + VB3
  claim_guard ..................... T13 re-check at dispatch time
  late_success .................... §3.2 T15/T15a, correlation C-1…C-4
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from raeg_common.fingerprint import fingerprint
from raeg_common.models import Capability, LookupMode, Observation
from raeg_gateway.transitions import Basis, State

# --------------------------------------------------------------------------- policy


@dataclass(frozen=True)
class Policy:
    mode: str = "STRICT"  # STRICT = Tier S (default) | BOUNDED = Tier B (conditional on B1)
    order: str = "verify_first"  # verify_first (default) | retry_first (ablation)
    max_dispatch: int = 3
    max_verify: int = 4
    attempt_ms: int = 1500  # T_att: d_n = claim_time + T_att
    verify_lease_ms: int = 2000
    lookup_timeout_ms: int = 1000
    margin_ms: int = 100  # m
    clock_skew_ms: int = 50  # ε (RAEG DB clock vs provider clock)
    max_settle_wait_ms: int = 10_000
    backoff_base_ms: int = 100
    max_backoff_ms: int = 1000
    min_send_budget_ms: int = 200
    send_deadline: bool = True  # False = /noDL control
    no_verify: bool = False  # ablation
    no_ttl: bool = False  # ablation
    no_horizon: bool = False  # ablation
    version: str = "raeg-policy-v0.3"

    def __post_init__(self) -> None:
        if self.mode not in ("STRICT", "BOUNDED") or self.order not in ("verify_first", "retry_first"):
            raise ValueError(f"invalid policy {self}")
        if self.max_dispatch < 1 or self.max_verify < 1:
            raise ValueError("budgets must be >= 1")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Policy:
        return cls(**data)


# --------------------------------------------------------------------------- inputs / outputs


@dataclass(frozen=True)
class AttemptView:
    attempt_no: int
    deadline: datetime
    segment_no: int
    basis: str


@dataclass(frozen=True)
class Snapshot:
    """The controller's view of one execution (subset of the executions row + attempts)."""

    action_key: str
    fingerprint: str
    state: State
    attempt_count: int
    verify_attempts: int
    segment_no: int
    anchor: datetime | None
    max_deadline: datetime | None
    absence_proven_through: datetime | None
    retry_basis: str | None
    provider_txn_id: str | None
    escalation_reason: str | None
    attempts: tuple[AttemptView, ...] = ()

    @classmethod
    def from_row(cls, row: dict[str, Any], attempts: list[dict[str, Any]]) -> Snapshot:
        return cls(
            action_key=row["action_key"],
            fingerprint=row["fingerprint"],
            state=State(row["state"]),
            attempt_count=row["attempt_count"],
            verify_attempts=row["verify_attempts"],
            segment_no=row["segment_no"],
            anchor=row["anchor"],
            max_deadline=row["max_deadline"],
            absence_proven_through=row["absence_proven_through"],
            retry_basis=row["retry_basis"],
            provider_txn_id=row["provider_txn_id"],
            escalation_reason=row["escalation_reason"],
            attempts=tuple(
                AttemptView(a["attempt_no"], a["deadline"], a["segment_no"], a["basis"]) for a in attempts
            ),
        )

    def to_json(self) -> dict[str, Any]:
        def ts(v: datetime | None) -> str | None:
            return v.isoformat() if v else None

        return {
            "action_key": self.action_key,
            "state": self.state.value,
            "attempt_count": self.attempt_count,
            "verify_attempts": self.verify_attempts,
            "segment_no": self.segment_no,
            "anchor": ts(self.anchor),
            "max_deadline": ts(self.max_deadline),
            "absence_proven_through": ts(self.absence_proven_through),
            "retry_basis": self.retry_basis,
            "provider_txn_id": self.provider_txn_id,
            "escalation_reason": self.escalation_reason,
            "attempts": [
                {"attempt_no": a.attempt_no, "deadline": ts(a.deadline), "segment_no": a.segment_no, "basis": a.basis}
                for a in self.attempts
            ],
        }


@dataclass(frozen=True)
class LookupView:
    ok: bool
    as_of: datetime | None
    records: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Decision:
    rule_id: str
    transition: str | None  # None = no state change (engine records a note decision)
    patch: dict[str, Any] = field(default_factory=dict)
    basis: Basis | None = None
    anomaly: str | None = None
    detail: str = ""

    @property
    def target(self) -> str | None:
        return self.transition


# Escalation reasons (DESIGN §2.10 R-E*, §2.10a VB2/VB4).
NO_SAFE_BASIS = "NO_SAFE_BASIS"
VERIFY_BUDGET_EXHAUSTED = "VERIFY_BUDGET_EXHAUSTED"
VERIFY_BUDGET_EXHAUSTED_BEFORE_HORIZON = "VERIFY_BUDGET_EXHAUSTED_BEFORE_HORIZON"
SETTLE_WAIT_EXCEEDS_POLICY = "SETTLE_WAIT_EXCEEDS_POLICY"
DISPATCH_BUDGET_EXHAUSTED = "DISPATCH_BUDGET_EXHAUSTED"
ABSENCE_UNPROVABLE = "ABSENCE_UNPROVABLE"
CONTRADICTORY_EVIDENCE = "CONTRADICTORY_EVIDENCE"
INTERNAL = "INTERNAL"


# --------------------------------------------------------------------------- predicates


def ms(n: int) -> timedelta:
    return timedelta(milliseconds=n)


def d_eff(c: Capability, p: Policy) -> bool:
    """§2.3: D_eff = D ∧ every attempt carried Request-Deadline (all or none, set by policy)."""
    return c.deadline_enforced and p.send_deadline


def horizon_enforced(c: Capability, p: Policy) -> bool:
    """Effect horizon H is a bound on every attempt's effect: by D_eff (Tier S) or by B1 (Tier B)."""
    return d_eff(c, p) or p.mode == "BOUNDED"


def lookup_usable(c: Capability, p: Policy) -> bool:
    return c.lookup == LookupMode.BY_CLIENT_REF and not p.no_verify


def settle_time(x: Snapshot, c: Capability) -> datetime | None:
    if x.max_deadline is None or c.lookup_lag_ms is None:
        return None
    return x.max_deadline + ms(c.lookup_lag_ms)


def absence_authoritative(x: Snapshot, as_of: datetime | None, c: Capability, p: Policy) -> bool:
    """§2.5: no succeeded record ∧ as_of ≥ H + δ ∧ (D_eff ∨ BOUNDED). The record check is the caller's."""
    if as_of is None:
        return False
    if p.no_horizon:  # ablation: any miss is believed
        return True
    settle = settle_time(x, c)
    return horizon_enforced(c, p) and settle is not None and as_of >= settle


def replay_ok(x: Snapshot, c: Capability, p: Policy, d_new: datetime) -> tuple[bool, str]:
    """§2.6: I ∧ (τ = ∞ ∨ ((D_eff ∨ BOUNDED) ∧ d_new + ε + m ≤ anchor + τ))."""
    if not c.idempotency:
        return False, "NO_IDEMPOTENCY"
    if c.key_ttl_ms is None:
        return True, "TAU_INFINITE"
    if p.no_ttl:
        return True, "ABLATION_NO_TTL"
    if not horizon_enforced(c, p):
        return False, "FINITE_TAU_WITHOUT_ENFORCED_HORIZON"
    if x.anchor is None:
        return False, "NO_ANCHOR"
    limit = x.anchor + ms(c.key_ttl_ms)
    if d_new + ms(p.clock_skew_ms + p.margin_ms) <= limit:
        return True, "WITHIN_RETENTION"
    return False, "RETENTION_WINDOW_CLOSED"


def segment_attempts(x: Snapshot) -> list[AttemptView]:
    return [a for a in x.attempts if a.segment_no == x.segment_no]


def single_processing(x: Snapshot, c: Capability, p: Policy) -> tuple[bool, str]:
    """Rule DD core (§2.8): every attempt other than the observed one is ruled out.
    Earlier segments are already proven absent, so only the current segment matters."""
    seg = segment_attempts(x)
    if len(seg) <= 1:
        return True, "SINGLE_ATTEMPT_IN_SEGMENT"
    if c.idempotency and c.key_ttl_ms is None:
        return True, "KEY_NEVER_EXPIRES"
    if c.idempotency and horizon_enforced(c, p) and x.anchor is not None and c.key_ttl_ms is not None:
        limit = x.anchor + ms(c.key_ttl_ms)
        if all(a.deadline + ms(p.clock_skew_ms + p.margin_ms) <= limit for a in seg):
            return True, "KEY_WITHIN_RETENTION_FOR_SEGMENT"
    return False, "OTHER_ATTEMPTS_NOT_RULED_OUT"


def backoff(n: int, p: Policy) -> timedelta:
    return ms(min(p.backoff_base_ms * (2 ** max(n - 1, 0)), p.max_backoff_ms))


# --------------------------------------------------------------------------- dispatch result (T3/T4/T5)


def on_dispatch_result(x: Snapshot, observation: Observation, c: Capability, p: Policy, *,
                       provider_txn_id: str | None = None, replayed: bool = False,
                       error_class: str | None = None) -> Decision:
    if observation == Observation.COMMITTED:  # rule DS: E ≥ 1 established
        return Decision("D-COMMITTED", "T3", {"provider_txn_id": provider_txn_id,
                                              "outcome_source": "replay" if replayed else "response"})
    if observation == Observation.DECLINED:
        ok, why = single_processing(x, c, p)
        if ok:
            return Decision("D-DECLINED-DD", "T4", {"outcome_source": "response"}, detail=why)
        return Decision("D-DECLINED-NOT-DEFINITIVE", "T5", {"ambiguity_cause": "DECLINE_NOT_DEFINITIVE"}, detail=why)
    if observation == Observation.DEADLINE_REJECTED:
        return Decision("D-DEADLINE-REJECTED", "T5", {"ambiguity_cause": "DEADLINE_REJECTED"})
    if observation == Observation.INTERNAL:
        return Decision("D-INTERNAL", "T5", {"ambiguity_cause": "INTERNAL", "escalation_reason": INTERNAL})
    return Decision("D-AMBIGUOUS", "T5", {"ambiguity_cause": error_class or "AMBIGUOUS"})


# --------------------------------------------------------------------------- decide (state UNKNOWN)


def decide(x: Snapshot, c: Capability, p: Policy, now: datetime, *, lookups_reached_horizon: bool = False) -> Decision:
    if x.state != State.UNKNOWN:
        raise ValueError(f"decide() requires UNKNOWN, got {x.state.value}")
    # Pending escalation recorded by T11 (L-EU / VB2) or T5 (INTERNAL).
    if x.escalation_reason:
        return Decision("R-E-PENDING", "T14", {"escalation_reason": x.escalation_reason}, detail=x.escalation_reason)

    d_new = now + ms(p.attempt_ms)
    can_dispatch = x.attempt_count < p.max_dispatch
    replay, replay_why = replay_ok(x, c, p, d_new) if can_dispatch else (False, "DISPATCH_BUDGET_EXHAUSTED")

    if p.order == "retry_first" and replay:
        return Decision("R-RI", "T12", {"retry_basis": Basis.IDEMPOTENT_REPLAY.value, "next_action_at": None},
                        basis=Basis.IDEMPOTENT_REPLAY, detail=f"retry_first:{replay_why}")

    if lookup_usable(c, p) and x.verify_attempts < p.max_verify:
        settle = settle_time(x, c)
        if settle is not None and now < settle and x.verify_attempts >= p.max_verify - 1:  # VB1 reserve
            if settle - now > ms(p.max_settle_wait_ms):  # VB2 configuration guard
                return Decision("R-E-VB2", "T14", {"escalation_reason": SETTLE_WAIT_EXCEEDS_POLICY})
            return Decision("R-V-VB1-WAIT", "TW", {"next_action_at": settle})
        return Decision("R-V", "T6", {"verify_attempts": x.verify_attempts + 1})

    if replay:
        return Decision("R-RI", "T12", {"retry_basis": Basis.IDEMPOTENT_REPLAY.value, "next_action_at": None},
                        basis=Basis.IDEMPOTENT_REPLAY, detail=replay_why)

    # R-E*: no safe basis remains (VB4 when the verify budget is the binding constraint).
    if lookup_usable(c, p) and x.verify_attempts >= p.max_verify:
        reason = VERIFY_BUDGET_EXHAUSTED if lookups_reached_horizon else VERIFY_BUDGET_EXHAUSTED_BEFORE_HORIZON
    elif not can_dispatch:
        reason = DISPATCH_BUDGET_EXHAUSTED
    else:
        reason = NO_SAFE_BASIS
    return Decision(f"R-E-{reason}", "T14", {"escalation_reason": reason}, detail=replay_why)


# --------------------------------------------------------------------------- on_lookup (state VERIFYING)


def on_lookup(x: Snapshot, r: LookupView, c: Capability, p: Policy, now: datetime) -> Decision:
    if x.state != State.VERIFYING:
        raise ValueError(f"on_lookup() requires VERIFYING, got {x.state.value}")
    if not r.ok:  # L-ERR / VB3: budget was consumed at T6
        return Decision("L-ERR", "T11", {"next_action_at": now + backoff(x.verify_attempts, p),
                                         "ambiguity_cause": "LOOKUP_ERROR"})
    mine = [rec for rec in r.records if rec.get("client_reference") == x.action_key]
    succeeded = [rec for rec in mine if rec.get("status") == "succeeded"]
    declined = [rec for rec in mine if rec.get("status") == "declined"]
    if succeeded:  # L-S
        return Decision("L-S", "T7", {"provider_txn_id": succeeded[0].get("provider_txn_id"),
                                      "outcome_source": "lookup", "duplicate_detected": len(succeeded) > 1},
                        anomaly="DUPLICATE_DETECTED" if len(succeeded) > 1 else None)

    if not absence_authoritative(x, r.as_of, c, p):
        if horizon_enforced(c, p):
            settle = settle_time(x, c)
            assert settle is not None
            if settle - now > ms(p.max_settle_wait_ms):  # VB2
                return Decision("L-W-VB2", "T11", {"escalation_reason": SETTLE_WAIT_EXCEEDS_POLICY})
            return Decision("L-W", "T11", {"next_action_at": settle, "ambiguity_cause": "AWAITING_SETTLEMENT"})
        if declined:
            ok, why = single_processing(x, c, p)
            if ok:
                return Decision("L-D1", "T8", {"outcome_source": "lookup"}, detail=why)
        return Decision("L-EU", "T11", {"escalation_reason": ABSENCE_UNPROVABLE})

    if declined:  # L-D2: authoritative, no success, a decline record exists
        return Decision("L-D2", "T8", {"outcome_source": "lookup"})
    if x.attempt_count < p.max_dispatch:  # L-RA: new segment
        return Decision("L-RA", "T10", {"retry_basis": Basis.VERIFIED_ABSENT.value,
                                        "absence_proven_through": x.max_deadline, "next_action_at": None},
                        basis=Basis.VERIFIED_ABSENT)
    return Decision("L-NE", "T9", {"absence_proven_through": x.max_deadline, "outcome_source": "lookup"})


# --------------------------------------------------------------------------- claim guard (T13)


def claim_guard(c: Capability, p: Policy):
    """Atomic re-check at claim time against the *new* deadline (store.claim calls this in-transaction)."""

    def guard(row: dict[str, Any], now: datetime, d_new: datetime) -> tuple[bool, str, str]:
        if row["attempt_count"] >= p.max_dispatch:
            return False, "G-BUDGET", "DISPATCH_BUDGET_EXHAUSTED"
        basis = row["retry_basis"]
        if basis == Basis.VERIFIED_ABSENT.value:
            ok = row["absence_proven_through"] is not None and row["max_deadline"] is not None \
                and row["absence_proven_through"] >= row["max_deadline"]
            return ok, "G-VA", "absence covers every earlier attempt" if ok else "absence proof stale"
        if basis == Basis.IDEMPOTENT_REPLAY.value:
            x = Snapshot.from_row(row, [])
            ok, why = replay_ok(x, c, p, d_new)
            return ok, "G-RI", why
        return False, "G-UNKNOWN-BASIS", str(basis)

    return guard


# --------------------------------------------------------------------------- late success (T15 / T15a)


PAYLOAD_FIELDS = ("amount_minor", "currency", "payer_id", "payee_id", "memo")


def correlate(x: Snapshot, attempt_no: int | None, body: Any) -> list[str]:
    """C-1…C-3 (C-4 holds by construction: the evidence row exists before this is called)."""
    failures = []
    if attempt_no is None or attempt_no not in {a.attempt_no for a in x.attempts}:
        failures.append("C-1: no RAEG attempt row for this response")
    if not isinstance(body, dict):
        return failures + ["C-2/C-3: response body unusable"]
    if body.get("client_reference") != x.action_key:
        failures.append("C-2: client_reference mismatch")
    try:
        echoed = fingerprint({f: body[f] for f in PAYLOAD_FIELDS})
    except (KeyError, ValueError):
        echoed = None
    if echoed != x.fingerprint:
        failures.append("C-2: echoed payload does not re-fingerprint to f")
    if body.get("status") != "succeeded" or not body.get("provider_txn_id"):
        failures.append("C-3: not a succeeded response with provider_txn_id")
    return failures


def late_success(x: Snapshot, attempt_no: int | None, body: Any) -> Decision | None:
    """Decision for one piece of late COMMITTED evidence. None = defer (action currently leased)."""
    failures = correlate(x, attempt_no, body)
    if failures:
        return Decision("LS-UNCORRELATED", None, anomaly="UNCORRELATED_LATE_RESPONSE", detail="; ".join(failures))
    deadline = next(a.deadline for a in x.attempts if a.attempt_no == attempt_no)
    contradicts = x.absence_proven_through is not None and deadline <= x.absence_proven_through
    txn = body.get("provider_txn_id")
    if x.state in (State.UNKNOWN, State.RETRY_WAIT):
        if contradicts:
            return Decision("LS-CONTRA", "T15a", {"escalation_reason": CONTRADICTORY_EVIDENCE},
                            anomaly=CONTRADICTORY_EVIDENCE, detail=f"attempt {attempt_no} deadline <= absence proof")
        return Decision("LS-OK", "T15", {"provider_txn_id": txn, "outcome_source": "late_response"})
    if x.state == State.SUCCEEDED:
        if txn != x.provider_txn_id:
            return Decision("LS-DUPLICATE", "TA", {"duplicate_detected": True}, anomaly="DUPLICATE_DETECTED",
                            detail=f"{txn} != {x.provider_txn_id}")
        return Decision("LS-CONSISTENT", None, detail="same provider_txn_id as the recorded outcome")
    if x.state in (State.DECLINED, State.NOT_EXECUTED):
        return Decision("LS-CONTRADICTS-TERMINAL", None, anomaly="CONTRADICTS_TERMINAL_CLAIM",
                        detail=f"late success after {x.state.value}")
    if x.state == State.ESCALATED:  # decision 7: operator-only; annotate
        return Decision("LS-FOR-ESCALATED", None, anomaly="LATE_SUCCESS_FOR_ESCALATED")
    return None  # PREPARED / DISPATCHING / VERIFYING: defer until lease-free
