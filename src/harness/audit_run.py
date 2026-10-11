"""Post-run audit (DESIGN §3.3, §7): writes audit.json for one run.

Mandatory checks (any failure marks the audit "fail"):
  completeness        every intent copy has a claim line; required raw files exist
  hooks_one_shot      every planned client hook fired at most once (fault equivalence across systems)
  oracle_cross_check  committed/declined receipts ↔ ledger rows, one-to-one
  raeg_store          (RAEG only) I2, I5, I6, I8, hash chains, attempts bookkeeping — live store
  write_ahead_I1      (RAEG only) every dispatch receipt matches an attempts row claimed before arrival
  replay_guard_I3     (RAEG only) every IDEMPOTENT_REPLAY attempt satisfied the §2.6 guard
  absence_I4          (RAEG only) every VERIFIED_ABSENT attempt follows an authoritative lookup
  safety_S            (RAEG, assumption set S) E ≤ 1 for every action
  claims_correct      (RAEG, assumption set S) SUCCESS ⇒ E ≥ 1; NO_EFFECT ⇒ E = 0
  raeg_terminal       (RAEG) no action non-terminal at the end of the run (§2.10a: a defect)
Informational (never fail the audit; these are data):
  effects_per_action  E per action from the oracle (for every system)
  faults_applied      planned vs applied provider rules and client hooks
  chain_heads         last event hash per action + a digest over all heads (anchors the hash chains)
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg

from raeg_gateway.audit import check_invariants

REQUIRED = ["manifest.json", "workload.jsonl", "fault_plan.json", "claims.jsonl", "driver_exchanges.jsonl",
            "oracle_ledger.csv", "provider_receipts.csv", "fault_log.csv", "provider_config.json"]


def _csv(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _check(ok: bool, details: Any = None) -> dict:
    return {"pass": bool(ok), "details": details}


async def audit(run_dir: Path, *, system_kind: str, assumption_set: str, raeg_admin_dsn: str | None,
                policy: dict | None, capability: dict | None) -> dict:
    out: dict[str, Any] = {"mandatory": {}, "informational": {}}
    m, info = out["mandatory"], out["informational"]
    workload = _jsonl(run_dir / "workload.jsonl")
    claims = _jsonl(run_dir / "claims.jsonl")
    plan = json.loads((run_dir / "fault_plan.json").read_text())
    ledger = _csv(run_dir / "oracle_ledger.csv")
    receipts = _csv(run_dir / "provider_receipts.csv")
    fault_log = _csv(run_dir / "fault_log.csv")

    expected = {(w["action_key"], c) for w in workload for c in range(w["copies"])}
    got = {(c["action_key"], c["copy"]) for c in claims}
    missing_files = [f for f in REQUIRED if not (run_dir / f).exists()]
    m["completeness"] = _check(expected == got and not missing_files,
                               {"missing_claims": sorted(map(list, expected - got))[:20], "missing_files": missing_files,
                                "n_intents": len(workload), "n_claim_lines": len(claims)})

    payment_by_receipt = {r["receipt_id"]: r for r in ledger}
    committed = {r["receipt_id"] for r in receipts if r.get("disposition") in ("committed", "declined")}
    mismatches = sorted(committed ^ set(payment_by_receipt))
    m["oracle_cross_check"] = _check(not mismatches, {"receipt_ledger_mismatches": mismatches[:20]})

    keys = [w["action_key"] for w in workload]
    effects = Counter(r["client_reference"] for r in ledger if r["status"] == "succeeded")
    e = {k: effects.get(k, 0) for k in keys}
    info["effects_per_action"] = {"histogram": dict(sorted(Counter(e.values()).items())), "by_action": e}

    planned_rules = {(r["client_reference"], r["kind"], int(r["ingress_index"]), r["fault"]) for r in plan["provider_rules"]}
    applied_rules = {(r["client_reference"], r["kind"], int(r["ingress_index"]), r["fault"]) for r in fault_log}
    hook_events = set()
    fire_counts: Counter = Counter()
    for path in (run_dir / "exchange_logs").glob("client_exchanges-*.jsonl") if (run_dir / "exchange_logs").exists() else []:
        for ev in _jsonl(path):
            if ev.get("event") in ("hook_fired", "pause_begin"):
                hook = (ev["action_key"], ev["attempt_no"], ev.get("point", "PAUSE"))
                hook_events.add(hook)
                fire_counts[hook] += 1
    refired = [[*h, n] for h, n in fire_counts.items() if n > 1]
    m["hooks_one_shot"] = _check(not refired, {"refired": refired})
    planned_hooks = {(h["action_key"], h["attempt_no"], h["point"]) for h in plan["hooks"]}
    info["faults_applied"] = {
        "provider_rules_planned": len(planned_rules), "provider_rules_applied": len(planned_rules & applied_rules),
        "provider_rules_not_applied": sorted(map(list, planned_rules - applied_rules))[:50],
        "hooks_planned": len(planned_hooks), "hooks_applied": len(planned_hooks & hook_events),
        "hooks_not_applied": sorted(map(list, planned_hooks - hook_events))[:50],
        "note": "a planned fault is not applied when the targeted ingress/attempt never happened",
    }

    if system_kind == "raeg":
        assert raeg_admin_dsn and policy and capability
        conn = await asyncpg.connect(raeg_admin_dsn)
        try:
            report = await check_invariants(conn)
            m["raeg_store"] = _check(not any(report.values()), {k: v[:20] for k, v in report.items() if v})
            heads = [(r["action_key"], r["seq"], r["hash"]) for r in await conn.fetch(
                "SELECT DISTINCT ON (action_key) action_key, seq, hash FROM execution_events ORDER BY action_key, seq DESC")]
            digest = hashlib.sha256("\n".join(f"{k}|{s}|{h}" for k, s, h in heads).encode()).hexdigest()
            info["chain_heads"] = {"n": len(heads), "sha256_of_heads": digest, "heads": [list(h) for h in heads]}
            attempts = [dict(r) for r in await conn.fetch("SELECT * FROM attempts ORDER BY action_key, attempt_no")]
            evidence = [dict(r) for r in await conn.fetch(
                "SELECT action_key, provider_as_of, received_at_db FROM evidence WHERE kind = 'lookup_response' AND provider_as_of IS NOT NULL")]
            states = {r["action_key"]: r["state"] for r in await conn.fetch("SELECT action_key, state FROM executions")}
        finally:
            await conn.close()

        by_attempt = {(a["action_key"], a["attempt_no"]): a for a in attempts}
        i1 = []
        for r in receipts:
            if r.get("kind") != "dispatch" or not r.get("attempt_hdr"):
                continue
            a = by_attempt.get((r["client_reference"], int(r["attempt_hdr"])))
            if a is None or a["claimed_at"] > _ts(r["arrived_at"]):
                i1.append([r["client_reference"], r["attempt_hdr"], r["arrived_at"]])
        m["write_ahead_I1"] = _check(not i1, {"violations": i1[:20]})

        eps_m = timedelta(milliseconds=policy["clock_skew_ms"] + policy["margin_ms"])
        tau = capability.get("key_ttl_ms")
        i3 = []
        segs: dict[tuple, list] = defaultdict(list)
        for a in attempts:
            segs[(a["action_key"], a["segment_no"])].append(a)
        for (k, _), seg in segs.items():
            anchor = seg[0]["claimed_at"]
            for a in seg:
                if a["basis"] == "IDEMPOTENT_REPLAY":
                    ok = capability["idempotency"] and (tau is None or policy.get("no_ttl")
                                                        or a["deadline"] + eps_m <= anchor + timedelta(milliseconds=tau))
                    if not ok:
                        i3.append([k, a["attempt_no"]])
        m["replay_guard_I3"] = _check(not i3, {"violations": i3})

        delta = timedelta(milliseconds=capability.get("lookup_lag_ms") or 0)
        lookups = defaultdict(list)
        for ev in evidence:
            lookups[ev["action_key"]].append(ev)
        i4 = []
        for a in attempts:
            if a["basis"] != "VERIFIED_ABSENT":
                continue
            prior = [b["deadline"] for b in attempts if b["action_key"] == a["action_key"] and b["attempt_no"] < a["attempt_no"]]
            need = max(prior) + delta if prior else None
            ok = policy.get("no_horizon") or (need is not None and any(
                ev["provider_as_of"] >= need and ev["received_at_db"] <= a["claimed_at"] for ev in lookups[a["action_key"]]))
            if not ok:
                i4.append([a["action_key"], a["attempt_no"]])
        m["absence_I4"] = _check(not i4, {"violations": i4})

        non_terminal = sorted(k for k in keys if states.get(k) not in ("SUCCEEDED", "DECLINED", "NOT_EXECUTED", "ESCALATED"))
        m["raeg_terminal"] = _check(not non_terminal, {"non_terminal": non_terminal[:20]})

        if assumption_set == "S":
            m["safety_S"] = _check(all(v <= 1 for v in e.values()), {"actions_with_E_ge_2": [k for k, v in e.items() if v >= 2]})
            primary = {c["action_key"]: c["claim"] for c in claims if c["copy"] == 0}
            wrong = [[k, cl, e.get(k)] for k, cl in primary.items()
                     if (cl == "SUCCESS" and e.get(k, 0) < 1) or (cl.startswith("NO_EFFECT") and e.get(k, 0) != 0)]
            m["claims_correct"] = _check(not wrong, {"violations": wrong[:20]})

    out["status"] = "pass" if all(c["pass"] for c in m.values()) else "fail"
    out["failed_checks"] = [name for name, c in m.items() if not c["pass"]]
    return out
