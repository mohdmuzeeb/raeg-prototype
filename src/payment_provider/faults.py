"""Deterministic provider-side fault plan (DESIGN §6.3).

Rules are keyed by (client_reference, kind, ingress_index): the nth request of that kind for that
reference arriving at the provider's ingress. Ingress counters are in-memory and the provider runs
as one process, so numbering follows ingress order. Every applied fault is written to fault_log.
"""

from __future__ import annotations

import enum
import threading
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FaultKind(str, enum.Enum):
    INGRESS_HOLD = "INGRESS_HOLD"  # F9/F10: delay before receipt (models delivery delay)
    LOST_RESPONSE = "LOST_RESPONSE"  # F1: commit, then reset (or blackhole) the response
    PRE_COMMIT_STALL = "PRE_COMMIT_STALL"  # F3: stall, then fail without committing
    DELAYED_COMMIT = "DELAYED_COMMIT"  # F4: commit only after a delay
    ERROR_5XX_PRE = "ERROR_5XX_PRE"  # 503 before commit
    FORCE_DECLINE = "FORCE_DECLINE"  # deterministic business decline
    LOOKUP_UNAVAILABLE = "LOOKUP_UNAVAILABLE"  # F8


class FaultRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    client_reference: str
    kind: str = Field(default="dispatch", pattern="^(dispatch|lookup_ref)$")
    ingress_index: int = Field(ge=1)
    fault: FaultKind
    params: dict[str, Any] = Field(default_factory=dict)


class FaultPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str = "none"
    rules: list[FaultRule] = Field(default_factory=list)


class FaultEngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plan = FaultPlan()
        self._index: dict[tuple[str, str, int], list[FaultRule]] = {}
        self._counters: dict[tuple[str, str], int] = defaultdict(int)

    @property
    def plan(self) -> FaultPlan:
        return self._plan

    def load(self, plan: FaultPlan) -> None:
        index: dict[tuple[str, str, int], list[FaultRule]] = defaultdict(list)
        for rule in plan.rules:
            index[(rule.client_reference, rule.kind, rule.ingress_index)].append(rule)
        with self._lock:
            self._plan = plan
            self._index = dict(index)

    def reset_counters(self) -> None:
        with self._lock:
            self._counters.clear()

    def next_ingress(self, client_reference: str | None, kind: str) -> tuple[int | None, dict[FaultKind, FaultRule]]:
        """Assign the ingress index for this arrival and return the faults that apply to it."""
        if client_reference is None:
            return None, {}
        with self._lock:
            self._counters[(client_reference, kind)] += 1
            n = self._counters[(client_reference, kind)]
            rules = self._index.get((client_reference, kind, n), [])
        return n, {rule.fault: rule for rule in rules}
