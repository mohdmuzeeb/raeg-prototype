"""Deterministic fault plans (DESIGN §6.3; keying per IMPLEMENTATION_NOTES N2).

A plan is generated once per (seed, workload, cell) and replayed unchanged for every system:
  * provider rules: keyed by (client_reference, kind, provider ingress index)
  * client hooks:   keyed by (action_key, attempt_no) in the shared provider client
  * duplicates:     F5 is realised by the workload's concurrent copies
  * violations:     V1–V4 are profile-level switches applied to the provider for the whole run
Every concrete parameter (stall, delay, hold, pause lengths) is derived from the shared timing in
config/systems.yaml so that the fault means the same thing for every system.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.workload import Intent

SINGLE_FAULTS = ("F1", "F2", "F3", "F4", "F6", "F7", "F8", "F9", "F10")
MIXTURE = ("F1", "F2", "F3", "F4", "F6", "F9")  # FM (DESIGN §6.3); F5 comes from workload duplicates
VIOLATIONS = {
    "V1": {"lookup_lag_exceeds_declared_ms": None},  # filled from δ below
    "V2": {"ignore_deadline": True},
    "V3": {"actual_retention_ms": None},  # filled from τ below
    "V4": {"clock_offset_ms": None},  # filled from ε below
}


@dataclass
class Timing:
    attempt_ms: int
    margin_ms: int
    lookup_lag_ms: int | None
    key_ttl_ms: int | None
    clock_skew_ms: int = 50


@dataclass
class FaultPlan:
    plan_id: str
    cell_fault: str
    fault_rate: float
    provider_rules: list[dict] = field(default_factory=list)
    hooks: list[dict] = field(default_factory=list)
    assignments: dict[str, str] = field(default_factory=dict)  # action_key -> fault id
    violations: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"plan_id": self.plan_id, "cell_fault": self.cell_fault, "fault_rate": self.fault_rate,
                "provider_rules": self.provider_rules, "hooks": self.hooks, "assignments": self.assignments,
                "violations": self.violations, "notes": self.notes}

    def provider_plan(self) -> dict:
        return {"plan_id": self.plan_id, "rules": self.provider_rules}

    def hook_plan(self) -> dict:
        return {"hooks": self.hooks}

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _rule(key: str, idx: int, fault: str, kind: str = "dispatch", **params) -> dict:
    return {"client_reference": key, "kind": kind, "ingress_index": idx, "fault": fault, "params": params}


def _apply(fault: str, key: str, t: Timing, rng: random.Random, plan: FaultPlan) -> None:
    timeout = t.attempt_ms - t.margin_ms  # every system's HTTP timeout for attempt 1
    if fault == "F1":  # lost response after commit
        plan.provider_rules.append(_rule(key, 1, "LOST_RESPONSE", mode="reset"))
    elif fault == "F2":  # crash after the response, before persisting it
        plan.hooks.append({"action_key": key, "attempt_no": 1, "point": "CP3"})
    elif fault == "F3":  # stall past every client timeout; never commits
        plan.provider_rules.append(_rule(key, 1, "PRE_COMMIT_STALL", stall_ms=t.attempt_ms + 1000))
    elif fault == "F4":  # commit after the client timeout but before the deadline
        plan.provider_rules.append(_rule(key, 1, "DELAYED_COMMIT", delay_ms=timeout + t.margin_ms // 2))
    elif fault == "F6":  # crash before send (CP1) or while in flight (CP2)
        point = rng.choice(["CP1", "CP2"])
        hook = {"action_key": key, "attempt_no": 1, "point": point}
        if point == "CP2":
            hook["after_send_ms"] = 5
        plan.hooks.append(hook)
    elif fault == "F7":  # zombie: pause between claim and send, longer than T_att
        plan.hooks.append({"action_key": key, "attempt_no": 1, "point": "PAUSE", "pause_ms": t.attempt_ms + 3500})
    elif fault == "F8":  # lookups unavailable (first three per action)
        plan.provider_rules.extend(_rule(key, i, "LOOKUP_UNAVAILABLE", kind="lookup_ref") for i in (1, 2, 3))
    elif fault == "F9":  # delivery delay: the original reaches the provider after a retry (and after d)
        plan.provider_rules.append(_rule(key, 1, "INGRESS_HOLD", hold_ms=t.attempt_ms + 1500))
    elif fault == "F10":  # retention-boundary overlap (short-τ profiles); sub-case by seed
        if t.key_ttl_ms is None:
            plan.notes.append(f"F10 skipped for {key}: profile has τ = ∞")
            return
        offset = rng.choice([-400, +400])
        plan.provider_rules.append(_rule(key, 1, "INGRESS_HOLD", hold_ms=t.attempt_ms + t.key_ttl_ms + offset))
    else:
        raise ValueError(f"unknown fault {fault}")


def generate(seed: int, intents: list[Intent], cell_fault: str, fault_rate: float, timing: Timing, plan_id: str) -> FaultPlan:
    rng = random.Random(f"faults|{seed}|{plan_id}")
    plan = FaultPlan(plan_id, cell_fault, fault_rate)
    if cell_fault in VIOLATIONS:
        v = dict(VIOLATIONS[cell_fault])
        if cell_fault == "V1":
            v["lookup_lag_exceeds_declared_ms"] = (timing.lookup_lag_ms or 0) * 3
        if cell_fault == "V3":
            v["actual_retention_ms"] = max((timing.key_ttl_ms or 0) // 3, 1)
        if cell_fault == "V4":
            v["clock_offset_ms"] = timing.clock_skew_ms * 20
        plan.violations = v
        base = "F1"  # V-cells combine the violation with lost responses so that recovery is exercised
    else:
        base = cell_fault
    if base in ("F0", "F5"):
        return plan
    for intent in intents:
        if rng.random() >= fault_rate:
            continue
        fault = rng.choice(MIXTURE) if base == "FM" else base
        plan.assignments[intent.action_key] = fault
        _apply(fault, intent.action_key, timing, rng, plan)
    return plan
