"""Harness logic without Docker: determinism of workload and fault plans, driver restart model,
and the audit's detection of incomplete/inconsistent raw directories."""

from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

import httpx
import pytest

from harness import fault_plan, workload
from harness.audit_run import audit
from harness.driver import Driver, DriverConfig

WL = workload.WorkloadConfig(n_actions=200, arrival_rate_per_s=10, duplicate_fraction=0.2, duplicate_copies=3)
TIMING = fault_plan.Timing(attempt_ms=1500, margin_ms=100, client_timeout_ms=600, lookup_lag_ms=500, key_ttl_ms=3000)


def test_workload_is_deterministic_and_seed_sensitive() -> None:
    a, b, c = workload.generate(1, WL, "ns"), workload.generate(1, WL, "ns"), workload.generate(2, WL, "ns")
    assert a == b and a != c
    assert len({i.action_key for i in a}) == 200
    assert all(x.arrival_offset_ms <= y.arrival_offset_ms for x, y in zip(a, a[1:]))
    dup = sum(i.copies > 1 for i in a) / len(a)
    assert 0.1 < dup < 0.3


def test_workload_roundtrip(tmp_path: Path) -> None:
    a = workload.generate(3, WL, "ns")
    workload.write(tmp_path / "w.jsonl", a)
    assert workload.read(tmp_path / "w.jsonl") == a


@pytest.mark.parametrize("fault", ["F1", "F2", "F3", "F4", "F6", "F7", "F8", "F9", "F10", "FM", "V1", "V2", "V3", "V4"])
def test_fault_plan_is_deterministic_and_system_independent(fault: str) -> None:
    intents = workload.generate(1, WL, "ns")
    p1 = fault_plan.generate(1, intents, fault, 0.3, TIMING, "ns")
    p2 = fault_plan.generate(1, intents, fault, 0.3, TIMING, "ns")
    assert p1.to_json() == p2.to_json(), "the same plan is replayed for every system"
    assert 0.15 < len(p1.assignments) / len(intents) < 0.45
    keys = {i.action_key for i in intents}
    assert {r["client_reference"] for r in p1.provider_rules} <= keys and {h["action_key"] for h in p1.hooks} <= keys
    if fault.startswith("V"):
        assert p1.violations and all(v is not None for v in p1.violations.values())
    if fault == "FM":
        assert set(p1.assignments.values()) <= set(fault_plan.MIXTURE) and len(set(p1.assignments.values())) > 3


def test_fault_parameters_follow_shared_timing() -> None:
    intents = workload.generate(1, WL, "ns")
    f4 = fault_plan.generate(1, intents, "F4", 1.0, TIMING, "ns")
    delay = f4.provider_rules[0]["params"]["delay_ms"]
    assert TIMING.client_timeout_ms < delay < TIMING.attempt_ms, "commit after client timeout, before d"
    f9 = fault_plan.generate(1, intents, "F9", 1.0, TIMING, "ns")
    hold = f9.provider_rules[0]["params"]["hold_ms"]
    assert TIMING.client_timeout_ms < hold < TIMING.attempt_ms, "held past the timeout (retry can overtake) but before d"
    f3 = fault_plan.generate(1, intents, "F3", 1.0, TIMING, "ns")
    assert f3.provider_rules[0]["params"]["stall_ms"] > TIMING.attempt_ms
    f7 = fault_plan.generate(1, intents, "F7", 1.0, TIMING, "ns")
    assert f7.hooks[0]["point"] == "PAUSE" and f7.hooks[0]["pause_ms"] > TIMING.attempt_ms


def test_f0_and_f5_plans_are_empty() -> None:
    intents = workload.generate(1, WL, "ns")
    for f in ("F0", "F5"):
        p = fault_plan.generate(1, intents, f, 1.0, TIMING, "ns")
        assert not p.provider_rules and not p.hooks and not p.assignments


# ----------------------------------------------------------------------------- driver


class FakeSystem:
    """Scripted per-key behaviour: a list of responses ('crash' = transport error)."""

    def __init__(self, script: dict[str, list]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = json.loads(request.content)["action_key"] if request.method == "POST" else request.url.path.rsplit("/", 1)[1]
        self.calls.append((request.method, key))
        step = self.script[key].pop(0) if len(self.script[key]) > 1 else self.script[key][0]
        if step == "crash":
            raise httpx.ConnectError("system down")
        return httpx.Response(200, json={"system": "X", "action_key": key, **step})


def intent(key: str, copies: int = 1) -> workload.Intent:
    return workload.Intent(0, key, 0, copies, {"amount_minor": 1, "currency": "USD", "payer_id": "p", "payee_id": "m", "memo": ""})


async def drive(tmp_path: Path, script: dict, intents: list, **cfg) -> list[dict]:
    fake = FakeSystem(script)
    d = Driver("http://sys", "X", "P1", DriverConfig(restart_backoff_ms=1, poll_ms=1, **cfg), tmp_path,
               transport=httpx.MockTransport(fake))
    out = await d.run(intents)
    return out, fake


async def test_driver_resubmits_after_crash(tmp_path: Path) -> None:
    out, fake = await drive(tmp_path, {"k": ["crash", "crash", {"status": "ok", "provider_txn_id": "t"}]}, [intent("k")])
    assert out[0]["claim"] == "SUCCESS" and out[0]["resubmits"] == 2
    assert [m for m, _ in fake.calls] == ["POST", "POST", "POST"]


async def test_driver_polls_pending_then_resubmits_when_state_lost(tmp_path: Path) -> None:
    script = {"k": [{"status": "pending"}, {"status": "pending", "detail": {"unknown_to_system": True}},
                    {"status": "error", "error_class": "ReadTimeout"}]}
    out, fake = await drive(tmp_path, script, [intent("k")])
    assert [m for m, _ in fake.calls] == ["POST", "GET", "POST"]
    assert out[0]["claim"] == "UNRESOLVED(gave_up_ambiguous)" and out[0]["resubmits"] == 1


async def test_driver_horizon_marks_unresolved(tmp_path: Path) -> None:
    out, _ = await drive(tmp_path, {"k": [{"status": "pending"}]}, [intent("k")], horizon_s=0.05)
    assert out[0]["claim"] == "UNRESOLVED(horizon)" and out[0]["latency_ms"] is None


async def test_driver_gives_up_after_max_resubmits(tmp_path: Path) -> None:
    out, fake = await drive(tmp_path, {"k": ["crash"]}, [intent("k")], max_resubmits=2)
    assert out[0]["claim"] == "UNRESOLVED(horizon)" and len(fake.calls) == 3


async def test_driver_concurrent_copies_and_logs(tmp_path: Path) -> None:
    out, fake = await drive(tmp_path, {"k": [{"status": "ok"}]}, [intent("k", copies=3)])
    assert sorted(o["copy"] for o in out) == [0, 1, 2] and len(fake.calls) == 3
    assert len((tmp_path / "claims.jsonl").read_text().splitlines()) == 3
    assert len((tmp_path / "driver_exchanges.jsonl").read_text().splitlines()) == 3


# ----------------------------------------------------------------------------- audit (non-RAEG raw dir)


def _write(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="") as fh:
        if rows:
            w = csv.DictWriter(fh, fieldnames=sorted(rows[0]))
            w.writeheader()
            w.writerows(rows)


def make_run_dir(tmp_path: Path, *, drop_claim: bool = False, orphan_payment: bool = False) -> Path:
    intents = [intent("a"), intent("b")]
    workload.write(tmp_path / "workload.jsonl", intents)
    fault_plan.FaultPlan("p", "F0", 0.0).write(tmp_path / "fault_plan.json")
    claims = [{"action_key": k, "copy": 0, "claim": "SUCCESS", "native": {"status": "ok"}} for k in ("a", "b")]
    (tmp_path / "claims.jsonl").write_text("\n".join(json.dumps(c) for c in claims[: 1 if drop_claim else 2]) + "\n")
    for name in ("manifest.json", "driver_exchanges.jsonl", "provider_config.json"):
        (tmp_path / name).write_text("{}")
    ledger = [{"receipt_id": "1", "client_reference": "a", "status": "succeeded"},
              {"receipt_id": "2", "client_reference": "b", "status": "succeeded"}]
    receipts = [{"receipt_id": "1", "kind": "dispatch", "disposition": "committed", "client_reference": "a", "attempt_hdr": "1"},
                {"receipt_id": "2", "kind": "dispatch", "disposition": "committed" if not orphan_payment else "error_5xx_pre",
                 "client_reference": "b", "attempt_hdr": "1"}]
    _write(tmp_path / "oracle_ledger.csv", ledger)
    _write(tmp_path / "provider_receipts.csv", receipts)
    (tmp_path / "fault_log.csv").write_text("")
    return tmp_path


async def test_audit_passes_complete_consistent_run(tmp_path: Path) -> None:
    report = await audit(make_run_dir(tmp_path), system_kind="retry", assumption_set="S", raeg_admin_dsn=None,
                         policy=None, capability={})
    assert report["status"] == "pass" and report["informational"]["effects_per_action"]["histogram"] == {1: 2}


async def test_audit_flags_a_hook_that_fired_twice(tmp_path: Path) -> None:
    run = make_run_dir(tmp_path)
    (run / "exchange_logs").mkdir()
    ev = {"event": "hook_fired", "action_key": "a", "attempt_no": 1, "point": "CP2"}
    (run / "exchange_logs" / "client_exchanges-B1.jsonl").write_text(json.dumps(ev) + "\n" + json.dumps(ev) + "\n")
    report = await audit(run, system_kind="retry", assumption_set="S", raeg_admin_dsn=None, policy=None, capability={})
    assert report["failed_checks"] == ["hooks_one_shot"]


async def test_audit_detects_missing_claim_and_ledger_mismatch(tmp_path: Path) -> None:
    report = await audit(make_run_dir(tmp_path, drop_claim=True, orphan_payment=True), system_kind="retry",
                         assumption_set="S", raeg_admin_dsn=None, policy=None, capability={})
    assert report["status"] == "fail" and set(report["failed_checks"]) == {"completeness", "oracle_cross_check"}
