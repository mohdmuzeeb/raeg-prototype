"""Create an immutable, checksummed archive of raw experiment runs (results/README.md, M7 Q1).

For the selected run directories this writes, inside a staging copy:
  SHA256SUMS            sha256 of every file in every run directory
then packs them into results/archive/<name>.tar.gz, writes <name>.tar.gz.sha256 next to it,
makes both read-only, and appends a line to docs/EXPERIMENT_LOG.md. Existing archives are never
overwritten. Raw run directories are left in place, unchanged.

usage: uv run python scripts/archive_raw.py <archive-name> <glob-of-run-dirs> [--note TEXT]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("pattern")
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    runs = sorted(p for p in (ROOT / "results" / "raw").glob(args.pattern) if p.is_dir())
    if not runs:
        print("no run directories matched", file=sys.stderr)
        return 2
    dest = ROOT / "results" / "archive" / f"{args.name}.tar.gz"
    if dest.exists():
        print(f"{dest} exists; archives are immutable", file=sys.stderr)
        return 3
    dest.parent.mkdir(parents=True, exist_ok=True)
    sums = []
    for run in runs:
        for f in sorted(p for p in run.rglob("*") if p.is_file()):
            sums.append(f"{sha256(f)}  {f.relative_to(ROOT / 'results' / 'raw')}")
    sums_path = dest.with_suffix("").with_suffix(".SHA256SUMS")
    sums_path.write_text("\n".join(sums) + "\n", encoding="utf-8")
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(sums_path, arcname=f"{args.name}/SHA256SUMS")
        for run in runs:
            tar.add(run, arcname=f"{args.name}/{run.name}")
    digest = sha256(dest)
    (dest.parent / f"{dest.name}.sha256").write_text(f"{digest}  {dest.name}\n", encoding="utf-8")
    for p in (dest, sums_path, dest.parent / f"{dest.name}.sha256"):
        os.chmod(p, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    log = ROOT / "docs" / "EXPERIMENT_LOG.md"
    if not log.exists():
        log.write_text("# Experiment log\n\nRaw results are archived locally (not in git). One row per archive.\n\n"
                       "| UTC | archive | sha256 | runs | files | size (bytes) | git HEAD | note |\n|---|---|---|---|---|---|---|---|\n",
                       encoding="utf-8")
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"| {datetime.now(UTC).isoformat(timespec='seconds')} | `results/archive/{dest.name}` | `{digest}` | "
                 f"{len(runs)} | {len(sums)} | {dest.stat().st_size} | `{head[:12]}` | {args.note} |\n")
    print(f"{dest} sha256={digest} runs={len(runs)} files={len(sums)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
