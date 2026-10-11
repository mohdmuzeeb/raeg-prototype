"""Pilot analysis (DESIGN §6.2, §8.3). Reads ONLY results/raw/<run>/ files; writes results/derived/<name>/.

Outputs (all regenerated deterministically from raw):
  runs.csv              one row per run: status, audit, wall clock, intents/accepted, claim counts (all and
                        accepted), E histogram, duplicates (E ≥ 2), false failure/success, faulted actions,
                        recovery completion, unresolved, latency p50/p95 (terminal answers only)
  safety.json           every RAEG run: audit safety_S / claims_correct / raeg_* checks; any E ≥ 2
  precision.json        recovery completion per (cell, system): per-seed values, between-seed variance, and the
                        projected 95% CI half-width for candidate (N, R)
Pilot data are excluded from paper tables (§8.3). Nothing here is a paper result.

usage: uv run python scripts/analyze_pilot.py <name> <glob-of-run-dirs>
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def jl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def analyze_run(d: Path) -> dict:
    m = json.loads((d / "manifest.json").read_text())
    rc = m["resolved_config"]
    row = {"run_id": m["run_id"], "cell": rc["cell_id"], "system": rc["system"], "seed": rc["seed"],
           "status": m["status"], "audit": m.get("audit_status"), "audit_failed": ";".join(m.get("audit_failed_checks") or []),
           "wall_clock_s": m.get("wall_clock_s"), "git_head": m["git"]["head"], "git_dirty": m["git"]["dirty"],
           "resolved_config_sha256": m.get("config_hashes", {}).get("resolved_config_sha256")}
    if not m["status"].startswith("complete"):
        return row
    claims = [c for c in jl(d / "claims.jsonl") if c["copy"] == 0]
    plan = json.loads((d / "fault_plan.json").read_text())
    ledger = list(csv.DictReader(open(d / "oracle_ledger.csv"))) if (d / "oracle_ledger.csv").stat().st_size else []
    effects = Counter(r["client_reference"] for r in ledger if r["status"] == "succeeded")
    e = {c["action_key"]: effects.get(c["action_key"], 0) for c in claims}

    def correct(c: dict) -> bool:
        k = c["claim"]
        return (k == "SUCCESS" and e[c["action_key"]] >= 1) or (k.startswith("NO_EFFECT") and e[c["action_key"]] == 0)

    faulted = [c for c in claims if c["action_key"] in plan["assignments"]]
    acc = [c for c in claims if c.get("accepted")]
    lat_all = [c["latency_ms"] for c in claims if c["latency_ms"] is not None]
    lat_f = [c["latency_ms"] for c in faulted if c["latency_ms"] is not None]
    row.update({
        "n_intents": len(claims), "n_accepted": len(acc), "n_faulted": len(faulted),
        "claims_all": json.dumps(dict(sorted(Counter(c["claim"] for c in claims).items()))),
        "claims_accepted": json.dumps(dict(sorted(Counter(c["claim"] for c in acc).items()))),
        "E_histogram": json.dumps(dict(sorted(Counter(e.values()).items()))),
        "duplicates": sum(1 for v in e.values() if v >= 2),
        "excess_effects": sum(max(0, v - 1) for v in e.values()),
        "false_failure": sum(1 for c in claims if c["claim"].startswith("NO_EFFECT") and e[c["action_key"]] >= 1),
        "false_success": sum(1 for c in claims if c["claim"] == "SUCCESS" and e[c["action_key"]] == 0),
        "unresolved": sum(1 for c in claims if c["claim"].startswith("UNRESOLVED")),
        "recovery_completed": sum(1 for c in faulted if correct(c)),
        "recovery_completion": (sum(1 for c in faulted if correct(c)) / len(faulted)) if faulted else None,
        "latency_p50_ms_all": pct(lat_all, 0.5), "latency_p95_ms_all": pct(lat_all, 0.95),
        "latency_p95_ms_faulted": pct(lat_f, 0.95),
    })
    return row


def main() -> int:
    name, pattern = sys.argv[1], sys.argv[2]
    out = ROOT / "results" / "derived" / name
    out.mkdir(parents=True, exist_ok=True)
    runs = [analyze_run(d) for d in sorted((ROOT / "results" / "raw").glob(pattern)) if (d / "manifest.json").exists()]
    cols = sorted({k for r in runs for k in r})
    with open(out / "runs.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(runs)

    safety = []
    for r in runs:
        if not r["system"].startswith("RAEG") or not r["status"].startswith("complete"):
            continue
        a = json.loads((ROOT / "results" / "raw" / r["run_id"] / "audit.json").read_text())
        safety.append({"run_id": r["run_id"], "audit": a["status"], "failed": a["failed_checks"],
                       "safety_S": a["mandatory"].get("safety_S", {}).get("pass"),
                       "claims_correct": a["mandatory"].get("claims_correct", {}).get("pass"),
                       "duplicates": r["duplicates"], "never_received": a["informational"].get("never_received_by_system", {}).get("n")})
    (out / "safety.json").write_text(json.dumps(safety, indent=1) + "\n")

    groups: dict[tuple, list] = defaultdict(list)
    for r in runs:
        if r.get("recovery_completion") is not None:
            groups[(r["cell"], r["system"])].append((r["seed"], r["recovery_completion"], r["n_faulted"]))
    precision = {"rule": "DESIGN §8.3: smallest R ≥ 5 with every faulted cell ≥ 100 faulted actions per seed and "
                         "projected 95% CI half-width of recovery completion ≤ 3 percentage points",
                 "model": "Var(mean over R seeds) = (between-seed variance + p(1-p)/n_faulted) / R; half-width = 1.96·sqrt(Var); "
                          "between-seed variance estimated from the pilot seeds (sample variance minus binomial part, floored at 0)",
                 "cells": {}}
    for (cell, system), vals in sorted(groups.items()):
        ps = [v for _, v, _ in vals]
        nf = statistics.mean(n for _, _, n in vals)
        p = statistics.mean(ps)
        s2 = statistics.variance(ps) if len(ps) > 1 else 0.0
        between = max(s2 - p * (1 - p) / nf, 0.0)
        proj = {}
        for n_actions in (200, 500, 1000):
            frac = nf / 200  # faulted fraction observed at N = 200
            nf_n = max(frac * n_actions, 1)
            for R in (3, 5, 10):
                hw = 1.96 * math.sqrt((between + p * (1 - p) / nf_n) / R)
                proj[f"N{n_actions}_R{R}"] = round(hw * 100, 2)
        precision["cells"][f"{cell}|{system}"] = {"seeds": [[s, round(v, 4), n] for s, v, n in vals], "mean": round(p, 4),
                                                  "between_seed_var": round(between, 6), "mean_faulted_per_run": nf,
                                                  "projected_halfwidth_pp": proj}
    (out / "precision.json").write_text(json.dumps(precision, indent=1, sort_keys=True) + "\n")
    print(f"{len(runs)} runs analysed -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
