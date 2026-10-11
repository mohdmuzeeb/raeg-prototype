"""Project an experiment's wall-clock runtime from *observed* calibration runs (M7 instruction).

Each target run (cell × seed × system of the target config) is mapped to the measured calibration run
of the closest representative, conservatively:
  system  -> representative family:  B1,B2 -> B1;  B3 -> B3;  B3L-* -> B3L-horizon;  RAEG-* -> RAEG-strict
  cell    -> calibration cell:       F0 -> the F0 cell; FC-worker/FC-api -> the measured FC cell;
                                     F1 and FM -> the slowest measured FM cell for that family (upper bound)
Per-run time = the measured run's total wall clock (bring-up + driver + drain + collect + audit), so
fixed overheads are included. Writes <out>.json with every input and mapping, and prints a summary.

usage: uv run python scripts/project_runtime.py <calibration-glob> <target-config.yaml> <out-path>
"""

from __future__ import annotations

import json
import sys
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

    def source(cell: dict, fam: str) -> float:
        """Conservative: F0 -> measured F0; FC-* -> measured FC-*; F1/FM -> max over measured FM cells."""
        if cell["fault"] == "F0":
            return measured[(f0_cell, fam)]["wall_clock_s"], [f0_cell]
        if cell["fault"] == "FC-worker":
            return measured[(fc_worker, fam)]["wall_clock_s"], [fc_worker]
        if cell["fault"] == "FC-api":
            return measured[(fc_api, fam)]["wall_clock_s"], [fc_api]
        vals = [(measured[(c, fam)]["wall_clock_s"], c) for c in fm_cells]
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
              "per_run": rows}
    out_path.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{result['n_runs']} runs, projected {result['projected_total_h']} h ({result['projected_total_s']} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
