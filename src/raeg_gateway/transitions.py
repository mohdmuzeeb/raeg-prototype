"""The execution state machine of DESIGN §3, as data. The store refuses anything not listed here."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class State(str, enum.Enum):
    PREPARED = "PREPARED"
    DISPATCHING = "DISPATCHING"
    UNKNOWN = "UNKNOWN"
    VERIFYING = "VERIFYING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    DECLINED = "DECLINED"
    NOT_EXECUTED = "NOT_EXECUTED"
    ESCALATED = "ESCALATED"


LEASED = frozenset({State.DISPATCHING, State.VERIFYING})
TERMINAL = frozenset({State.SUCCEEDED, State.DECLINED, State.NOT_EXECUTED})  # absorbing
UNRESOLVED_TERMINAL = frozenset({State.ESCALATED})  # leaves only via T16 (operator)


class Basis(str, enum.Enum):
    """Why a dispatch is permitted (DESIGN §2.6, §2.10). INITIAL and VERIFIED_ABSENT start a segment."""

    INITIAL = "INITIAL"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    VERIFIED_ABSENT = "VERIFIED_ABSENT"

    @property
    def starts_segment(self) -> bool:
        return self in (Basis.INITIAL, Basis.VERIFIED_ABSENT)


@dataclass(frozen=True)
class Transition:
    id: str
    sources: frozenset[State]
    target: State
    description: str


def _t(tid: str, sources: set[State], target: State, description: str) -> Transition:
    return Transition(tid, frozenset(sources), target, description)


S = State
TRANSITIONS: dict[str, Transition] = {
    t.id: t
    for t in [
        _t("T2", {S.PREPARED}, S.DISPATCHING, "claim first attempt (write-ahead)"),
        _t("T3", {S.DISPATCHING}, S.SUCCEEDED, "COMMITTED response"),
        _t("T4", {S.DISPATCHING}, S.DECLINED, "DECLINED response satisfying DD"),
        _t("T5", {S.DISPATCHING}, S.UNKNOWN, "ambiguous / deadline-rejected / decline without DD / lease expired"),
        _t("T6", {S.UNKNOWN}, S.VERIFYING, "R-V: start lookup"),
        _t("T7", {S.VERIFYING}, S.SUCCEEDED, "L-S"),
        _t("T8", {S.VERIFYING}, S.DECLINED, "L-D1 / L-D2"),
        _t("T9", {S.VERIFYING}, S.NOT_EXECUTED, "L-NE"),
        _t("T10", {S.VERIFYING}, S.RETRY_WAIT, "L-RA: verified absent, new segment"),
        _t("T11", {S.VERIFYING}, S.UNKNOWN, "wait / lookup error / unprovable / lease expired"),
        _t("T12", {S.UNKNOWN}, S.RETRY_WAIT, "R-RI: idempotent replay approved"),
        _t("T13", {S.RETRY_WAIT}, S.DISPATCHING, "claim retry; guard re-checked against new deadline"),
        _t("T13a", {S.RETRY_WAIT}, S.UNKNOWN, "retry guard failed at claim"),
        _t("T14", {S.UNKNOWN}, S.ESCALATED, "R-E*"),
        _t("T15", {S.UNKNOWN, S.RETRY_WAIT}, S.SUCCEEDED, "correlated late success"),
        _t("T15a", {S.UNKNOWN, S.RETRY_WAIT}, S.ESCALATED, "late success contradicts proven absence"),
        _t("T16s", {S.ESCALATED}, S.SUCCEEDED, "operator resolution"),
        _t("T16d", {S.ESCALATED}, S.DECLINED, "operator resolution"),
        _t("T16n", {S.ESCALATED}, S.NOT_EXECUTED, "operator resolution"),
    ]
}
# T1 (creation) is not a state-to-state transition; the store handles it in submit().
CLAIM_TRANSITIONS = frozenset({"T2", "T13"})


def check_transition(tid: str, current: State) -> Transition:
    t = TRANSITIONS.get(tid)
    if t is None:
        raise IllegalTransition(f"unknown transition {tid}")
    if current not in t.sources:
        raise IllegalTransition(f"{tid} not allowed from {current.value} (allowed: {sorted(s.value for s in t.sources)})")
    return t


class IllegalTransition(Exception):
    pass


# Columns a transition patch may set. Identity columns are fixed at creation (and by DB grants).
MUTABLE_COLUMNS = frozenset(
    {
        "attempt_count",
        "verify_attempts",
        "segment_no",
        "anchor",
        "max_deadline",
        "absence_proven_through",
        "next_action_at",
        "lease_owner",
        "lease_expires_at",
        "retry_basis",
        "provider_txn_id",
        "outcome_source",
        "ambiguity_cause",
        "escalation_reason",
        "duplicate_detected",
        "pending_late_success",
    }
)
IDENTITY_COLUMNS = ("action_key", "fingerprint", "payload", "profile_id", "capability", "policy", "created_at")
SYSTEM_COLUMNS = ("state", "version", "updated_at")
ALL_COLUMNS = (*IDENTITY_COLUMNS, *SYSTEM_COLUMNS, *sorted(MUTABLE_COLUMNS))
TIMESTAMP_COLUMNS = frozenset(
    {"created_at", "updated_at", "anchor", "max_deadline", "absence_proven_through", "next_action_at", "lease_expires_at"}
)
