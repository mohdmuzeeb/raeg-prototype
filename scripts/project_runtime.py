"""Project an experiment's wall-clock runtime from *observed* calibration runs (M7 instruction).

Each target run (cell × seed × system of the target config) is mapped to the measured calibration run
of the closest representative, conservatively:
  system  -> representative family:  B1,B2 -> B1;  B3 -> B3;  B3L-* -> B3L-horizon;  RAEG-* -> RAEG-strict
  cell    -> calibration cell:       F0 -> the F0 cell; FC-worker/FC-api -> the measured FC cell;
                                     F1 and FM -> the slowest measured FM cell for that family (upper bound)
Per-run time = the measured run's total wall clock (bring-up + driver + drain + collect + audit), so
fixed overheads are included. Writes <out>.json with every input and mapping, and prints a summary.

With --bring-up-from <glob> (runs measured after a start-up change, N56): each mapped run's measured
`bring_up` phase is replaced by the MAXIMUM `bring_up` observed in those runs for the same stack
(Temporal for B3/B3L, otherwise non-Temporal), and the maximum observed gap between consecutive runs
(finished_at -> next created_at) is added per run. Everything else stays the measured value.

usage: uv run python scripts/project_runtime.py <calibration-glob> <target-config.yaml> <out-path>
                                                [--bring-up-from <glob>]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FAMILY = {"B1": "B1", "B2": "B1", "B3": "B3", "B3-noDL": "B3"}


def family(system: str) -> str:
    if system in FAMILY:
        return FAMILY[system]
    if system.startswith("B3L"):
        return "B3L-horizon"
    if system.startswith("RAEG"):
        return "RAEG-strict"
    raise ValueError(system)


def main() -> int:
    calib_glob, target_cfg, out_path = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    bring_up_glob = sys.argv[sys.argv.index("--bring-up-from") + 1] if "--bring-up-from" in sys.argv else None
    bring_up, gap_s, bring_up_runs = None, 0.0, []
    if bring_up_glob:
        obs: dict[bool, list[float]] = {True: [], False: []}
        spans = []
        for d in sorted((ROOT / "results" / "raw").glob(bring_up_glob)):
            m = json.loads((d / "manifest.json").read_text())
            temporal = family(m["resolved_config"]["system"]) in ("B3", "B3L-horizon")
            obs[temporal].append(m["phase_timings_s"]["bring_up"])
            spans.append((m["created_at"], m["finished_at"]))
            bring_up_runs.append({"run_id": m["run_id"], "status": m["status"], "phases": m["phase_timings_s"]})
        bring_up = {k: max(v) for k, v in obs.items()}
        spans.sort()
        gaps = [(datetime.fromisoformat(b[0]) - datetime.fromisoformat(a[1])).total_seconds() for a, b in zip(spans, spans[1:])]
        gap_s = max(gaps) if gaps else 0.0
    measured: dict[tuple[str, str], dict] = {}
    for d in sorted((ROOT / "results" / "raw").glob(calib_glob)):
        m = json.loads((d / "manifest.json").read_text())
        rc = m["resolved_config"]
        key = (rc["cell_id"], rc["system"])
        measured[key] = {"run_id": m["run_id"], "status": m["status"], "wall_clock_s": m.get("wall_clock_s"),
                         "phases": m.get("phase_timings_s"), "audit": m.get("audit_status")}
    def measured_cell(fault_prefix: str, profile: str | None = None) -> str | None:
        hits = sorted({c for c, _ in measured if f"-{fault_prefix}-r" in c and (profile is None or c.startswith(profile + "-"))})
        return hits[0] if hits else None

    f0_cell = measured_cell("F0")
    fm_cells = sorted({c for c, _ in measured if "-FM-" in c})
    fc_worker = measured_cell("FC-worker")
    fc_api = measured_cell("FC-api")

    def secs(cell_id: str, fam: str) -> float:
        rec = measured[(cell_id, fam)]
        if bring_up is None:
            return rec["wall_clock_s"]
        new_bu = bring_up[fam in ("B3", "B3L-horizon")]
        return rec["wall_clock_s"] - rec["phases"]["bring_up"] + new_bu + gap_s

    def source(cell: dict, fam: str) -> float:
        """Conservative: F0 -> measured F0; FC-* -> measured FC-*; F1/FM -> max over measured FM cells."""
        if cell["fault"] == "F0":
            return secs(f0_cell, fam), [f0_cell]
        if cell["fault"] == "FC-worker":
            return secs(fc_worker, fam), [fc_worker]
        if cell["fault"] == "FC-api":
            return secs(fc_api, fam), [fc_api]
        vals = [(secs(c, fam), c) for c in fm_cells]
        best = max(vals)
        return best[0], [best[1]]

    cfg = yaml.safe_load(target_cfg.read_text())
    rows, total = [], 0.0
    for cell in cfg["cells"]:
        cell_id = f"{cell['profile']}-{cell['fault']}-r{int(cell['fault_rate'] * 100):03d}"
        for system in cfg["systems"]:
            secs, src = source(cell, family(system))
            for seed in cell.get("seeds") or cfg["seeds"]:
                rows.append({"cell": cell_id, "system": system, "seed": seed, "from": [src, family(system)],
                             "projected_s": secs})
                total += secs
    result = {"target": str(target_cfg), "n_runs": len(rows), "projected_total_s": round(total, 1),
              "projected_total_h": round(total / 3600, 2), "calibration_runs": {f"{c}|{s}": v for (c, s), v in measured.items()},
              "mapping": {"F0": f0_cell, "F1/FM (max over)": fm_cells, "FC-worker": fc_worker, "FC-api": fc_api},
              "bring_up_override": None if bring_up is None else {
                  "from": bring_up_glob, "max_bring_up_s": {"temporal": bring_up[True], "non_temporal": bring_up[False]},
                  "max_inter_run_gap_s": gap_s, "runs": bring_up_runs},
              "per_run": rows}
    out_path.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{result['n_runs']} runs, projected {result['projected_total_h']} h ({result['projected_total_s']} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
