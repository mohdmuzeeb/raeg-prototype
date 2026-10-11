"""Paper analysis of the M7 dataset (280 runs), computed ONLY from the frozen raw archive.

Steps (all deterministic):
  1. Check the archive's SHA-256 against the expected value (EXPERIMENTAL_RESULTS.md).
  2. Extract it to a fresh temporary directory and verify EVERY file against the archive's SHA256SUMS.
  3. Check every run: status complete, audit pass, the expected git commit, a clean tree.
  4. Recompute the metrics (DESIGN §6.2) per (profile, fault, system) from claims.jsonl (the agent's
     claim, copy 0), oracle_ledger.csv (E = number of succeeded provider effects per action),
     fault_plan.json (which actions had an injected fault) and workload.jsonl (F5 duplicate submissions).
  5. Write paper/m7/: metrics.json, tables (Markdown + LaTeX), figures (PDF + PNG), provenance.json.

Confidence intervals (exploratory; R = 3 seeds per cell): 95% two-stage cluster bootstrap (resample seeds
with replacement, then actions within each drawn seed; equivalently a binomial draw per drawn seed),
B = 10000, numpy default_rng(20261011). Because a bootstrap over only 3 seeds can collapse to zero width,
the exact Clopper-Pearson 95% interval over all pooled actions is also computed and the WIDER of the two
is reported (the method used is recorded per estimate).

Original precision target (DESIGN §8.3, not relaxed): R >= 5 seeds, >= 100 faulted actions per seed, and
95% CI half-width of recovery completion <= 3 percentage points. Every estimate is checked against it.

Temporal-based systems (B3, B3L-delta, B3L-horizon): latency is INVALID for comparison (cold start inside
the measured window, IMPLEMENTATION_NOTES N58). Their latency is never tabulated or plotted as a value.

usage: uv run --group analysis python scripts/analyze_paper.py [archive] [--expect-sha256 HEX] [--out DIR]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import sys
import tarfile
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import beta

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "results" / "archive" / "pilot-20261011.tar.gz"
EXPECT_SHA256 = "66da0511c5879d988e49d1915461fdd3c29ab8744afde50253351d62192ac23a"
EXPECT_COMMIT = "235ffd00124b"  # prefix of the commit every M7 pilot run recorded (pre-sanitisation SHA)
PUBLISHED_COMMIT = "6c7a2a7446905f1acb7392003b389da7515821a6"  # same code, published SHA (docs/COMMIT_MAP.md)
B = 10_000
RNG_SEED = 20261011
TARGET = {"R_min": 5, "faulted_per_seed_min": 100, "halfwidth_pp_max": 3.0}
SYSTEMS = ["B1", "B2", "B3", "B3L-delta", "B3L-horizon", "RAEG-strict", "RAEG-bounded"]
LABEL = {"B1": "B1 naive retry", "B2": "B2 idempotency-only", "B3": "B3 Temporal", "B3L-delta": "B3L-δ",
         "B3L-horizon": "B3L-horizon", "RAEG-strict": "RAEG-strict", "RAEG-bounded": "RAEG-bounded"}
TEMPORAL = {"B3", "B3L-delta", "B3L-horizon"}
PROFILES = ["P1", "P2", "P4", "P6"]
PROFILE_DESC = {"P1": "I, τ=∞, lookup, D", "P2": "I, short τ, lookup, D", "P4": "no I, lookup, D",
                "P6": "no I, no lookup, no D"}
FAULTS = ["F0", "F1", "FM"]


# --------------------------------------------------------------------------- archive verification
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_verified(archive: Path, expect: str, dest: Path) -> tuple[Path, dict]:
    got = sha256_file(archive)
    if got != expect:
        sys.exit(f"archive sha256 mismatch: expected {expect}, got {got}")
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(dest, filter="data")
    (top,) = [p for p in dest.iterdir() if p.is_dir()]
    sums = (top / "SHA256SUMS").read_text().splitlines()
    bad = []
    for line in sums:
        digest, rel = line.split(maxsplit=1)
        if sha256_file(top / rel) != digest:
            bad.append(rel)
    if bad:
        sys.exit(f"{len(bad)} files fail SHA256SUMS, e.g. {bad[:3]}")
    return top, {"archive": archive.name, "sha256": got, "files_verified": len(sums)}


# --------------------------------------------------------------------------- per-action records
def jl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def load_run(d: Path) -> tuple[dict, list[dict]]:
    m = json.loads((d / "manifest.json").read_text())
    plan = json.loads((d / "fault_plan.json").read_text())
    copies = {w["action_key"]: w["copies"] for w in jl(d / "workload.jsonl")}
    text = (d / "oracle_ledger.csv").read_text()
    ledger = list(csv.DictReader(io.StringIO(text))) if text.strip() else []
    effects = Counter(r["client_reference"] for r in ledger if r["status"] == "succeeded")
    acts = []
    for c in jl(d / "claims.jsonl"):
        if c["copy"] != 0:  # the agent's claim is copy 0 (N41); other copies are F5 duplicate submissions
            continue
        k, e = c["claim"], effects.get(c["action_key"], 0)
        acts.append({"key": c["action_key"], "E": e, "claim": k, "accepted": bool(c.get("accepted")),
                     "faulted": c["action_key"] in plan["assignments"],
                     "fault": plan["assignments"].get(c["action_key"]),
                     "f5": copies[c["action_key"]] > 1, "latency_ms": c["latency_ms"], "arrival_ms": c["arrival_rel_ms"],
                     "duplicate": e >= 2, "excess": max(0, e - 1),
                     "unresolved": k.startswith("UNRESOLVED"),
                     "false_failure": k.startswith("NO_EFFECT") and e >= 1,
                     "false_success": k == "SUCCESS" and e == 0,
                     "decided": k == "SUCCESS" or k.startswith("NO_EFFECT"),
                     "correct": (k == "SUCCESS" and e >= 1) or (k.startswith("NO_EFFECT") and e == 0)})
    if len({a["key"] for a in acts}) != len(acts):
        sys.exit(f"duplicate copy-0 claims in {d.name}")
    return m, acts


# --------------------------------------------------------------------------- intervals
def clopper_pearson(x: int, n: int) -> tuple[float, float]:
    lo = 0.0 if x == 0 else float(beta.ppf(0.025, x, n - x + 1))
    hi = 1.0 if x == n else float(beta.ppf(0.975, x + 1, n - x))
    return lo, hi


def estimate(per_seed: list[tuple[int, int]], rng: np.random.Generator) -> dict | None:
    """per_seed: [(successes, trials)] per seed. Pooled proportion with the reported 95% CI."""
    per_seed = [(x, n) for x, n in per_seed if n > 0]
    if not per_seed:
        return None
    xs = np.array([x for x, _ in per_seed])
    ns = np.array([n for _, n in per_seed])
    x, n = int(xs.sum()), int(ns.sum())
    R = len(per_seed)
    picks = rng.integers(0, R, size=(B, R))
    draws = rng.binomial(ns[picks], (xs / ns)[picks])
    stats = draws.sum(axis=1) / ns[picks].sum(axis=1)
    b_lo, b_hi = float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))
    c_lo, c_hi = clopper_pearson(x, n)
    lo, hi, method = (b_lo, b_hi, "cluster bootstrap") if (b_hi - b_lo) >= (c_hi - c_lo) else (c_lo, c_hi, "Clopper-Pearson")
    return {"x": x, "n": n, "p": x / n, "ci": [lo, hi], "halfwidth_pp": (hi - lo) / 2 * 100, "method": method,
            "per_seed": [[int(a), int(b)] for a, b in per_seed], "R": R}


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


# --------------------------------------------------------------------------- formatting
def fmt_p(e: dict | None, digits: int = 1) -> str:
    if e is None:
        return "–"
    return f"{e['p'] * 100:.{digits}f}% [{e['ci'][0] * 100:.{digits}f}, {e['ci'][1] * 100:.{digits}f}]"


def fmt_frac(e: dict | None) -> str:
    return "–" if e is None else f"{e['x']}/{e['n']}"


def md_table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def tex_escape(s: str) -> str:
    return (s.replace("\\", r"\textbackslash{}").replace("%", r"\%").replace("&", r"\&").replace("_", r"\_")
            .replace("#", r"\#").replace("δ", r"$\delta$").replace("τ", r"$\tau$").replace("∞", r"$\infty$")
            .replace("≥", r"$\geq$").replace("–", "--").replace("✗", r"$\times$").replace("<", r"$<$").replace(">", r"$>$")
            .replace("φ", r"$\varphi$").replace("≤", r"$\leq$").replace("§", r"\S{}").replace("≈", r"$\approx$")
            .replace("×", r"$\times$").replace("—", "---"))


def tex_table(header: list[str], rows: list[list[str]], caption: str, label: str) -> str:
    caption = re.sub(r"^Table \S+\.\s+", "", caption)  # LaTeX numbers tables itself
    # manuscript captions carry no repository-internal references (kept in the Markdown tables for traceability)
    caption = re.sub(r"\s*\((IMPLEMENTATION_NOTES )?N\d+\)", "", caption).replace("DESIGN §8.3: ", "")
    cols = "l" * 2 + "r" * (len(header) - 2)
    body = [r"\begin{table*}[t]", r"\centering", r"\scriptsize", rf"\caption{{{tex_escape(caption)}}}", rf"\label{{{label}}}",
            rf"\begin{{tabular}}{{{cols}}}", r"\toprule", " & ".join(tex_escape(h) for h in header) + r" \\", r"\midrule"]
    body += [" & ".join(tex_escape(re.sub(r"\s*\((IMPLEMENTATION_NOTES )?N\d+\)", "", c)) for c in r) + r" \\" for r in rows]
    body += [r"\bottomrule", r"\end{tabular}", r"\end{table*}"]
    return "\n".join(body) + "\n"


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("archive", nargs="?", default=str(ARCHIVE))
    ap.add_argument("--expect-sha256", default=EXPECT_SHA256)
    ap.add_argument("--out", default=str(ROOT / "paper" / "m7"))
    args = ap.parse_args()
    out = Path(args.out)
    (out / "tables").mkdir(parents=True, exist_ok=True)
    (out / "figures").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(RNG_SEED)

    with tempfile.TemporaryDirectory() as tmp:
        top, archive_info = extract_verified(Path(args.archive), args.expect_sha256, Path(tmp))
        run_dirs = sorted(p for p in top.iterdir() if p.is_dir())
        runs, data = [], defaultdict(dict)  # data[(profile, fault, system)][seed] = actions
        hosts, commits, statuses, audits, dirty = set(), Counter(), Counter(), Counter(), Counter()
        raeg_audit = []
        for d in run_dirs:
            m, acts = load_run(d)
            rc = m["resolved_config"]
            cell = rc["cell"]
            key = (cell["profile"], cell["fault"], rc["system"])
            data[key][rc["seed"]] = acts
            statuses[m["status"]] += 1
            audits[m.get("audit_status")] += 1
            commits[m["git"]["head"][:12]] += 1
            dirty[m["git"]["dirty"]] += 1
            hosts.add((m["host"]["cpu"], m["host"]["machine"], int(m["host"]["mem_bytes"]) // 2**30,
                       m["host"]["platform"].split("-")[0], m["docker"]["server_version"], m["docker"]["context"],
                       m["python"].split()[0]))
            a = json.loads((d / "audit.json").read_text())
            if rc["system"].startswith("RAEG"):
                raeg_audit.append({"run": m["run_id"], "status": a["status"],
                                   "safety_S": a["mandatory"]["safety_S"]["pass"],
                                   "claims_correct": a["mandatory"]["claims_correct"]["pass"],
                                   "write_ahead_I1": a["mandatory"]["write_ahead_I1"]["pass"],
                                   "replay_guard_I3": a["mandatory"]["replay_guard_I3"]["pass"],
                                   "absence_I4": a["mandatory"]["absence_I4"]["pass"],
                                   "raeg_terminal": a["mandatory"]["raeg_terminal"]["pass"],
                                   "never_received": a["informational"]["never_received_by_system"]["n"],
                                   "accepted_dup": sum(1 for x in acts if x["accepted"] and x["duplicate"]),
                                   "accepted_wrong": sum(1 for x in acts if x["accepted"] and x["decided"] and not x["correct"])})
            runs.append({"run_id": m["run_id"], "profile": cell["profile"], "fault": cell["fault"],
                         "system": rc["system"], "seed": rc["seed"], "status": m["status"],
                         "audit": m.get("audit_status"), "n": len(acts)})

    # Integrity of the dataset itself: refuse to analyse anything unexpected.
    problems = []
    if len(runs) != 280:
        problems.append(f"expected 280 runs, found {len(runs)}")
    if set(statuses) != {"complete"}:
        problems.append(f"non-complete runs: {dict(statuses)}")
    if set(audits) != {"pass"}:
        problems.append(f"audit not pass: {dict(audits)}")
    if set(commits) != {EXPECT_COMMIT} or set(dirty) != {False}:
        problems.append(f"commit/dirty mismatch: {dict(commits)} {dict(dirty)}")
    if problems:
        sys.exit("; ".join(problems))

    # ---------------------------------------------------------------- per-cell metrics
    cells = {}
    for (prof, fault, system), by_seed in sorted(data.items()):
        seeds = sorted(by_seed)
        A = [by_seed[s] for s in seeds]

        def est(pred, subset=lambda a: True):
            return estimate([(sum(1 for a in acts if subset(a) and pred(a)), sum(1 for a in acts if subset(a))) for acts in A], rng)

        faulted = [sum(a["faulted"] for a in acts) for acts in A]
        dup_actions = [a for acts in A for a in acts if a["duplicate"]]
        lat = [a["latency_ms"] for acts in A for a in acts if a["latency_ms"] is not None]
        lat_f = [a["latency_ms"] for acts in A for a in acts if a["faulted"] and a["latency_ms"] is not None]
        e = {
            "profile": prof, "fault": fault, "system": system, "seeds": seeds, "R": len(seeds),
            "intents": sum(len(x) for x in A), "accepted": sum(a["accepted"] for acts in A for a in acts),
            "faulted_per_seed": faulted,
            "duplicate_rate": est(lambda a: a["duplicate"]),
            "excess_effects": sum(a["excess"] for acts in A for a in acts),
            "duplicate_causes": dict(Counter(("F5 concurrent submission" if a["f5"] else "") + (" + " if a["f5"] and a["faulted"] else "")
                                             + (f"fault {a['fault']}" if a["faulted"] else "") or "no fault, no F5"
                                             for a in dup_actions)),
            "recovery_completion": est(lambda a: a["correct"], lambda a: a["faulted"]) if sum(faulted) else None,
            "unresolved_rate": est(lambda a: a["unresolved"]),
            "unresolved_rate_accepted": est(lambda a: a["unresolved"], lambda a: a["accepted"]),
            "unresolved_reasons": dict(Counter(a["claim"] for acts in A for a in acts if a["unresolved"])),
            "false_success": sum(a["false_success"] for acts in A for a in acts),
            "false_failure": sum(a["false_failure"] for acts in A for a in acts),
            "false_success_rate": est(lambda a: a["false_success"]),
            "false_failure_rate": est(lambda a: a["false_failure"]),
            "claim_correctness": est(lambda a: a["correct"], lambda a: a["decided"]),
            "decided": sum(a["decided"] for acts in A for a in acts),
            "claims": dict(Counter(a["claim"] for acts in A for a in acts)),
            "latency_valid": system not in TEMPORAL,
            "latency_ms": None if system in TEMPORAL else {"p50": pct(lat, .5), "p95": pct(lat, .95), "p99": pct(lat, .99),
                                                          "n": len(lat), "faulted_p95": pct(lat_f, .95), "faulted_n": len(lat_f)},
        }
        rec = e["recovery_completion"]
        if rec is not None:
            e["precision_target"] = {
                "R": len(seeds), "R_met": len(seeds) >= TARGET["R_min"],
                "min_faulted_per_seed": min(faulted), "faulted_met": min(faulted) >= TARGET["faulted_per_seed_min"],
                "halfwidth_pp": rec["halfwidth_pp"], "halfwidth_met": rec["halfwidth_pp"] <= TARGET["halfwidth_pp_max"]}
            e["precision_target"]["met"] = all(e["precision_target"][k] for k in ("R_met", "faulted_met", "halfwidth_met"))
        cells[f"{prof}|{fault}|{system}"] = e

    # ---------------------------------------------------------------- tables
    def row_tuple(e: dict) -> list[str]:
        return [e["profile"], LABEL[e["system"]], f"{e['accepted']}/{e['intents']}",
                f"{fmt_frac(e['duplicate_rate'])} ({fmt_p(e['duplicate_rate'])})", str(e["excess_effects"]),
                "–" if e["recovery_completion"] is None else f"{fmt_frac(e['recovery_completion'])} ({fmt_p(e['recovery_completion'])})",
                f"{fmt_frac(e['unresolved_rate'])} ({fmt_p(e['unresolved_rate'])})",
                str(e["false_success"]), str(e["false_failure"]),
                f"{fmt_frac(e['claim_correctness'])}"]

    head = ["Provider", "System", "Accepted/intents", "Duplicate-effect actions (rate, 95% CI)", "Excess effects",
            "Recovery completion (faulted actions, 95% CI)", "Unresolved (rate, 95% CI)", "False success",
            "False failure", "Correct/decided claims"]
    md_all = ["# M7 results tables (exploratory; generated by scripts/analyze_paper.py — do not edit)\n",
              f"Archive `{archive_info['archive']}` sha256 `{archive_info['sha256']}`; {archive_info['files_verified']} files verified. "
              "Counts are pooled over 3 seeds (seeds 1–3; 200 intents per run). Rates are per intent; recovery completion is per "
              "faulted action. 95% CIs: two-stage cluster bootstrap over seeds, or Clopper–Pearson when wider. "
              "No estimate meets the original precision target (Table 5).\n"]
    for i, fault in enumerate(FAULTS, start=1):
        rows = [row_tuple(cells[f"{p}|{fault}|{s}"]) for p in PROFILES for s in SYSTEMS]
        cap = {"F0": "Table 1a. No injected faults (F0). Duplicates arise only from F5 concurrent duplicate submissions (5% of intents).",
               "F1": "Table 1b. Lost responses (F1, φ = 0.2).",
               "FM": "Table 1c. Fault mixture FM (F1, F3, F4, F9 at φ = 0.2; faults-v2) plus F5."}[fault]
        md_all.append(f"## {cap}\n\n" + md_table(head, rows))
        (out / "tables" / f"table1_{fault}.tex").write_text(tex_table(head, rows, cap, f"tab:m7-{fault.lower()}"))

    # Table 2: safety audit
    t2_rows = []
    for s in ("RAEG-strict", "RAEG-bounded"):
        r = [x for x in raeg_audit if x["run"].split("_")[-2] == s]
        t2_rows.append([s, str(len(r)), f"{sum(x['status'] == 'pass' for x in r)}/{len(r)}",
                        f"{sum(x['safety_S'] for x in r)}/{len(r)}", f"{sum(x['claims_correct'] for x in r)}/{len(r)}",
                        f"{sum(x['write_ahead_I1'] and x['replay_guard_I3'] and x['absence_I4'] for x in r)}/{len(r)}",
                        f"{sum(x['raeg_terminal'] for x in r)}/{len(r)}", str(sum(x["accepted_dup"] for x in r)),
                        str(sum(x["accepted_wrong"] for x in r)), str(sum(x["never_received"] for x in r))])
    t2_head = ["System", "Runs", "Audit pass", "Tier S safety", "Claims correct", "Invariants I1, I3, I4", "All received actions terminal",
               "Accepted actions with E ≥ 2", "Accepted actions with wrong claim", "Intents never received"]
    cap2 = "Table 2. Post-run audit of every RAEG run (all faults, including FC-worker and FC-api)."
    md_all.append(f"## {cap2}\n\n" + md_table(t2_head, t2_rows))
    (out / "tables" / "table2_raeg_audit.tex").write_text(tex_table(t2_head, t2_rows, cap2, "tab:m7-audit"))

    # Table 3: duplicate causes (F1 and FM, pooled over profiles)
    t3_rows = []
    for s in SYSTEMS:
        for fault in FAULTS:
            c = Counter()
            for p in PROFILES:
                c.update(cells[f"{p}|{fault}|{s}"]["duplicate_causes"])
            if c:
                t3_rows.append([LABEL[s], fault, str(sum(c.values())), "; ".join(f"{k}: {v}" for k, v in sorted(c.items()))])
    t3_head = ["System", "Faults", "Duplicate-effect actions", "Cause of the action's exposure"]
    cap3 = "Table 3. Duplicate-effect actions by exposure (pooled over P1, P2, P4, P6 and seeds 1–3)."
    md_all.append(f"## {cap3}\n\n" + md_table(t3_head, t3_rows))
    (out / "tables" / "table3_duplicate_causes.tex").write_text(tex_table(t3_head, t3_rows, cap3, "tab:m7-dupcause"))

    # Table 4: latency (F0 overhead; Temporal invalid). Warm-up evidence (N58), measured per Temporal F0 run:
    # the arrival time of the last action whose latency exceeded 1 s.
    warm = []
    for (p, fault, s), by_seed in data.items():
        if fault == "F0" and s in TEMPORAL:
            for acts in by_seed.values():
                slow = [a["arrival_ms"] for a in acts if a["latency_ms"] is not None and a["latency_ms"] > 1000]
                warm.append(max(slow) / 1000 if slow else None)
    affected = [w for w in warm if w is not None]
    warmup = {"temporal_f0_runs": len(warm), "runs_with_actions_over_1s": len(affected),
              "last_slow_arrival_s_range": [round(min(affected), 1), round(max(affected), 1)] if affected else None}
    t4_rows = []
    for p in PROFILES:
        for s in SYSTEMS:
            e = cells[f"{p}|F0|{s}"]
            if not e["latency_valid"]:
                t4_rows.append([p, LABEL[s], "invalid (N58)", "invalid (N58)", "invalid (N58)", "–"])
            else:
                L = e["latency_ms"]
                t4_rows.append([p, LABEL[s], f"{L['p50']:.0f}", f"{L['p95']:.0f}", f"{L['p99']:.0f}", str(L["n"])])
    t4_head = ["Provider", "System", "p50 (ms)", "p95 (ms)", "p99 (ms)", "Actions"]
    cap4 = ("Table 4. Driver-observed latency without faults (F0), submit to terminal answer, pooled over seeds. "
            f"Temporal-based systems (B3, B3L) are marked invalid: the Temporal stack was not yet ready when the driver "
            f"started (IMPLEMENTATION_NOTES N58); in {warmup['runs_with_actions_over_1s']} of {warmup['temporal_f0_runs']} "
            f"Temporal F0 runs, actions arriving in the first {warmup['last_slow_arrival_s_range'][0]}–"
            f"{warmup['last_slow_arrival_s_range'][1]} s took over 1 s. RAEG includes a ≤20 ms worker poll (N52).")
    md_all.append(f"## {cap4}\n\n" + md_table(t4_head, t4_rows))
    (out / "tables" / "table4_latency_f0.tex").write_text(tex_table(t4_head, t4_rows, cap4, "tab:m7-latency"))

    # Table 5: precision assessment
    t5_rows, failing = [], 0
    for p in PROFILES:
        for fault in ("F1", "FM"):
            for s in SYSTEMS:
                e = cells[f"{p}|{fault}|{s}"]
                t = e["precision_target"]
                failing += not t["met"]
                why = [w for w, ok in (("R<5", t["R_met"]), ("faulted<100/seed", t["faulted_met"]),
                                       ("half-width>3pp", t["halfwidth_met"])) if not ok]
                t5_rows.append([p, fault, LABEL[s], str(t["R"]), str(t["min_faulted_per_seed"]),
                                f"{t['halfwidth_pp']:.1f}", e["recovery_completion"]["method"],
                                "met" if t["met"] else "✗ " + ", ".join(why)])
    t5_head = ["Provider", "Faults", "System", "Seeds R", "Min faulted actions per seed", "CI half-width (pp)", "CI method",
               "Original precision target"]
    cap5 = (f"Table 5. Recovery completion against the original precision target (DESIGN §8.3: R ≥ 5, ≥ 100 faulted "
            f"actions per seed, 95% CI half-width ≤ 3 pp). {failing} of {len(t5_rows)} estimates fail it; the target was not relaxed.")
    md_all.append(f"## {cap5}\n\n" + md_table(t5_head, t5_rows))
    (out / "tables" / "table5_precision.tex").write_text(tex_table(t5_head, t5_rows, cap5, "tab:m7-precision"))

    # Table 6: crash experiments (1 seed; exploratory)
    t6_rows = []
    for fault in ("FC-worker", "FC-api"):
        for p in ("P1", "P4"):
            for s in SYSTEMS:
                e = cells[f"{p}|{fault}|{s}"]
                t6_rows.append([p, fault, LABEL[s], f"{e['accepted']}/{e['intents']}", fmt_frac(e["duplicate_rate"]),
                                str(e["excess_effects"]),
                                "–" if e["recovery_completion"] is None else fmt_frac(e["recovery_completion"]),
                                fmt_frac(e["unresolved_rate"]), str(e["false_success"]), str(e["false_failure"])])
    t6_head = ["Provider", "Crash", "System", "Accepted/intents", "Duplicate-effect actions", "Excess effects",
               "Crash-targeted actions recovered", "Unresolved", "False success", "False failure"]
    cap6 = ("Table 6. Crash calibration cells (one seed, 3 crashes per run, 3 arrivals/s). FC-worker crashes the process "
            "executing the attempt (for B1/B2 this is their API process); FC-api crashes the driver-facing API process. "
            "Counts only: one seed supports no interval estimate across seeds.")
    md_all.append(f"## {cap6}\n\n" + md_table(t6_head, t6_rows))
    (out / "tables" / "table6_crash.tex").write_text(tex_table(t6_head, t6_rows, cap6, "tab:m7-crash"))
    (out / "tables" / "tables.md").write_text("\n".join(md_all))

    # ---------------------------------------------------------------- figures
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.size": 8, "pdf.fonttype": 42, "svg.hashsalt": "raeg"})
    markers = dict(zip(SYSTEMS, ["o", "s", "^", "v", "D", "*", "P"]))
    colors = dict(zip(SYSTEMS, plt.get_cmap("tab10").colors[:7]))

    def save(fig, name: str) -> None:
        fig.savefig(out / "figures" / f"{name}.pdf", bbox_inches="tight", metadata={"CreationDate": None, "ModDate": None})
        fig.savefig(out / "figures" / f"{name}.png", dpi=200, bbox_inches="tight", metadata={"Software": None})
        plt.close(fig)

    short = {"B1": "B1", "B2": "B2", "B3": "B3", "B3L-delta": "B3L-δ", "B3L-horizon": "B3L-h",
             "RAEG-strict": "RAEG-s", "RAEG-bounded": "RAEG-b"}
    captions = {}
    for fault in ("F1", "FM"):
        fig, axes = plt.subplots(2, 2, figsize=(7.0, 5.0), sharex=True, sharey=True, layout="constrained")
        for ax, p in zip(axes.flat, PROFILES):
            at = defaultdict(list)  # coincident points are labelled, never jittered
            for s in SYSTEMS:
                e = cells[f"{p}|{fault}|{s}"]
                u, d = e["unresolved_rate"], e["duplicate_rate"]
                at[(round(u["p"] * 100, 3), round(d["p"] * 100, 3))].append(short[s])
                ax.errorbar(u["p"] * 100, d["p"] * 100,
                            xerr=[[(u["p"] - u["ci"][0]) * 100], [(u["ci"][1] - u["p"]) * 100]],
                            yerr=[[(d["p"] - d["ci"][0]) * 100], [(d["ci"][1] - d["p"]) * 100]],
                            fmt=markers[s], color=colors[s], ms=6, capsize=2, lw=0.8, label=LABEL[s], alpha=0.85)
            band = Counter()  # stagger labels of points in the same 5-pp vertical band
            for (x, y), names in sorted(at.items()):
                i = band[int(y // 5)]
                band[int(y // 5)] += 1
                ax.annotate(", ".join(names), (x, y), xytext=(5, 7 + 11 * i), textcoords="offset points", fontsize=6.5,
                            arrowprops={"arrowstyle": "-", "lw": 0.4, "color": "0.5"})
            ax.set_title(f"{p}: {PROFILE_DESC[p]}", fontsize=8)
            ax.set_xlim(-2, 30)
            ax.set_ylim(-2, 30)
            ax.grid(alpha=0.3)
        for ax in axes[1]:
            ax.set_xlabel("Unresolved outcomes (% of intents)")
        for ax in axes[:, 0]:
            ax.set_ylabel("Duplicate-effect actions (% of intents)")
        handles, labels = axes[0, 0].get_legend_handles_labels()
        fig.legend(handles, labels, fontsize=7, loc="outside lower center", ncol=4, frameon=False)
        save(fig, f"fig1_tradeoff_{fault}")
        captions[f"fig1_tradeoff_{fault}"] = (
            f"Safety–completion trade-off under {fault} per provider profile. Each point pools 3 seeds × 200 intents; "
            "bars are 95% CIs (cluster bootstrap over seeds, or Clopper–Pearson when wider). Labels list the systems "
            "at each point; points are never jittered. Exploratory: no estimate meets the original precision target.")

    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.8), sharey=True, layout="constrained")
    width = 0.11
    for ax, fault in zip(axes, ("F1", "FM")):
        for j, s in enumerate(SYSTEMS):
            xs, ys, lo, hi = [], [], [], []
            for i, p in enumerate(PROFILES):
                r = cells[f"{p}|{fault}|{s}"]["recovery_completion"]
                xs.append(i + (j - 3) * width)
                ys.append(r["p"] * 100)
                lo.append((r["p"] - r["ci"][0]) * 100)
                hi.append((r["ci"][1] - r["p"]) * 100)
            ax.bar(xs, ys, width, yerr=[lo, hi], capsize=1.5, color=colors[s], label=LABEL[s], error_kw={"lw": 0.6})
            for x, y in zip(xs, ys):
                if y == 0:  # a 0% bar is invisible: label it
                    ax.annotate("0", (x, 0), xytext=(0, 9), textcoords="offset points", ha="center", fontsize=6,
                                color=colors[s])
        ax.set_xticks(range(len(PROFILES)), PROFILES)
        ax.set_title(fault, fontsize=8)
        ax.set_ylim(0, 105)
        ax.grid(axis="y", alpha=0.3)
    axes[0].set_ylabel("Recovery completion (% of faulted actions)")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, fontsize=7, loc="outside lower center", ncol=4, frameon=False)
    save(fig, "fig2_recovery_completion")
    captions["fig2_recovery_completion"] = (
        "Recovery completion per provider: share of faulted actions with a correct SUCCESS or NO_EFFECT claim by the "
        "horizon. A duplicated success counts as correct, so this figure must be read with Fig. 1. "
        "RAEG on P6 is 0: it escalated every faulted action (UNRESOLVED), as no safe recovery exists without "
        "idempotency, lookup or deadlines. 95% CIs as in Fig. 1; exploratory (R = 3).")
    (out / "figures" / "captions.md").write_text("\n".join(f"**{k}**: {v}\n" for k, v in captions.items()))

    # ---------------------------------------------------------------- machine-readable outputs
    (cpu, machine, mem_gib, os_name, docker_v, ctx, py) = sorted(hosts)[0]
    provenance = {
        "archive": archive_info, "runs": len(runs), "statuses": dict(statuses), "audits": dict(audits),
        "git_commit_of_runs": EXPECT_COMMIT, "git_tree_dirty_in_any_run": bool(dirty.get(True)),
        "environment": {"cpu": cpu, "arch": machine, "memory_gib": mem_gib, "os": os_name,
                        "docker_server": docker_v, "docker_context": ctx, "python": py, "distinct_hosts": len(hosts)},
        "ci": {"method": "two-stage cluster bootstrap (seeds, then actions) or Clopper-Pearson, whichever is wider",
               "B": B, "rng_seed": RNG_SEED, "level": 0.95},
        "precision_target": TARGET, "estimates_failing_target": failing, "estimates_checked": len(t5_rows),
        "latency_invalid_systems": sorted(TEMPORAL), "temporal_warmup_evidence": warmup,
    }
    (out / "provenance.json").write_text(json.dumps(provenance, indent=1, sort_keys=True) + "\n")
    (out / "metrics.json").write_text(json.dumps({"cells": cells, "raeg_audit": raeg_audit, "runs": runs},
                                                 indent=1, sort_keys=True, default=float) + "\n")
    # ---------------------------------------------------------------- generated block of EXPERIMENTAL_RESULTS.md
    hdr = ["System", "Intents", "Accepted", "Duplicate-effect actions", "Excess effects", "Faulted actions recovered",
           "Unresolved", "False success", "False failure", "Correct / decided claims"]
    rows = []
    for s_ in SYSTEMS:
        es = [cells[f"{p}|{f}|{s_}"] for p in PROFILES for f in ("F1", "FM")]
        rows.append([LABEL[s_], str(sum(e["intents"] for e in es)), str(sum(e["accepted"] for e in es)),
                     str(sum(e["duplicate_rate"]["x"] for e in es)), str(sum(e["excess_effects"] for e in es)),
                     f"{sum(e['recovery_completion']['x'] for e in es)}/{sum(e['recovery_completion']['n'] for e in es)}",
                     str(sum(e["unresolved_rate"]["x"] for e in es)), str(sum(e["false_success"] for e in es)),
                     str(sum(e["false_failure"] for e in es)),
                     f"{sum(e['claim_correctness']['x'] for e in es)}/{sum(e['claim_correctness']['n'] for e in es)}"])
    per_prof = []
    for p in PROFILES:
        for s_ in SYSTEMS:
            es = [cells[f"{p}|{f}|{s_}"] for f in ("F1", "FM")]
            per_prof.append([p, LABEL[s_], f"{sum(e['duplicate_rate']['x'] for e in es)}/{sum(e['intents'] for e in es)}",
                             f"{sum(e['recovery_completion']['x'] for e in es)}/{sum(e['recovery_completion']['n'] for e in es)}",
                             f"{sum(e['unresolved_rate']['x'] for e in es)}/{sum(e['intents'] for e in es)}"])
    strict = [x for x in raeg_audit if x["run"].split("_")[-2] == "RAEG-strict"]
    gen = ["<!-- BEGIN GENERATED by scripts/analyze_paper.py; do not edit by hand -->",
           f"Source: `{archive_info['archive']}` (sha256 `{archive_info['sha256']}`), {archive_info['files_verified']} files "
           f"verified; {len(runs)} runs, all `complete` with audit `pass`, all recording commit `{EXPECT_COMMIT}` with a clean tree "
           f"(published as `{PUBLISHED_COMMIT[:12]}`, identical source tree; see `docs/COMMIT_MAP.md`).",
           "", "**Pooled counts over P1, P2, P4, P6 × {F1, FM} × seeds 1–3** (24 runs and 4,800 intents per system). "
           "These are sums across heterogeneous cells, not population estimates; see `paper/m7/tables/tables.md` for per-cell "
           "rates with 95% CIs.", "", md_table(hdr, rows),
           "**Per provider (F1 and FM pooled; 6 runs, 1,200 intents per row)**: duplicate-effect actions / intents, "
           "recovered / faulted actions, unresolved / intents.", "", md_table(["Provider", "System", "Duplicate-effect", "Recovered",
                                                                               "Unresolved"], per_prof),
           f"**RAEG post-run audit** (all {len(raeg_audit)} RAEG runs incl. crash cells): Tier S safety passed in "
           f"{sum(x['safety_S'] for x in raeg_audit)}/{len(raeg_audit)}, claims correct in "
           f"{sum(x['claims_correct'] for x in raeg_audit)}/{len(raeg_audit)}; accepted actions with E ≥ 2: "
           f"{sum(x['accepted_dup'] for x in raeg_audit)}; accepted actions with a wrong claim: "
           f"{sum(x['accepted_wrong'] for x in raeg_audit)}. RAEG-strict alone: {len(strict)} runs, Tier S safety passed in "
           f"{sum(x['safety_S'] for x in strict)}/{len(strict)}.",
           "", f"**Precision target** (R ≥ 5, ≥ 100 faulted actions per seed, half-width ≤ 3 pp): failed by {failing} of "
           f"{len(t5_rows)} recovery-completion estimates (R = 3 everywhere; faulted actions per seed "
           f"{min(min(e['faulted_per_seed']) for e in cells.values() if e['recovery_completion'] and e['fault'] in ('F1', 'FM'))}–"
           f"{max(max(e['faulted_per_seed']) for e in cells.values() if e['recovery_completion'] and e['fault'] in ('F1', 'FM'))}).",
           "", f"**Temporal warm-up (latency invalid for B3, B3L-δ, B3L-horizon)**: in {warmup['runs_with_actions_over_1s']} of "
           f"{warmup['temporal_f0_runs']} Temporal F0 runs, actions arriving in the first {warmup['last_slow_arrival_s_range'][0]}–"
           f"{warmup['last_slow_arrival_s_range'][1]} s took over 1 s.",
           "<!-- END GENERATED -->"]
    res = ROOT / "EXPERIMENTAL_RESULTS.md"
    if res.exists():
        text = res.read_text()
        a, b = text.index("<!-- BEGIN GENERATED"), text.index("<!-- END GENERATED -->") + len("<!-- END GENERATED -->")
        res.write_text(text[:a] + "\n".join(gen) + text[b:])

    print(f"verified {archive_info['files_verified']} files; {len(runs)} runs; {len(cells)} cells -> {out}; "
          f"{failing}/{len(t5_rows)} recovery estimates fail the original precision target")
    return 0


if __name__ == "__main__":
    sys.exit(main())
