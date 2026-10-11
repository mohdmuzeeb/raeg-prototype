"""Run a pytest suite and preserve its evidence under results/tests/<UTC timestamp>_<suite>/.

Saved: manifest.json (provenance + exit status), pytest.log (full output), junit.xml,
and, with --compose-logs, the docker compose logs of the named services.
Failed runs are kept and marked as failed; nothing is deleted or overwritten.

usage: uv run python scripts/run_tests.py <suite> [--compose-logs svc ...] -- <pytest args>
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harness.manifest import build_manifest, write_manifest  # noqa: E402


def main() -> int:
    argv = sys.argv[1:]
    pytest_args: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, pytest_args = argv[:split], argv[split + 1 :]
    parser = argparse.ArgumentParser()
    parser.add_argument("suite")
    parser.add_argument("--compose-logs", nargs="*", default=[])
    args = parser.parse_args(argv)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "results" / "tests" / f"{stamp}_{args.suite}"
    out.mkdir(parents=True, exist_ok=False)
    manifest = build_manifest("test", {"suite": args.suite, "pytest_args": pytest_args, "status": "running"})
    write_manifest(out / "manifest.json", manifest)

    cmd = [sys.executable, "-m", "pytest", "-v", "-rA", "--tb=long", f"--junitxml={out / 'junit.xml'}", *pytest_args]
    with open(out / "pytest.log", "w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(cmd)}\n\n")
        log.flush()
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            log.write(line)
        code = proc.wait()

    if args.compose_logs:
        logs = subprocess.run(
            ["docker", "compose", "logs", "--no-color", "--timestamps", *args.compose_logs],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        (out / "compose.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")

    manifest["status"] = "passed" if code == 0 else f"failed(exit={code})"
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    write_manifest(out / "manifest.json", manifest)
    print(f"\n[run_tests] evidence saved to {out.relative_to(ROOT)} status={manifest['status']}")
    return code


if __name__ == "__main__":
    sys.exit(main())
