"""Collect every raw artefact of a run into results/raw/<run_id>/ (DESIGN §7). Nothing is summarised here."""

from __future__ import annotations

import csv
import json
import os
import subprocess
from pathlib import Path

import asyncpg
import httpx

ROOT = Path(__file__).resolve().parents[2]
ORACLE_URL = os.environ.get("ORACLE_URL", "http://127.0.0.1:19001")
ORACLE_TOKEN = os.environ.get("ORACLE_TOKEN", "local-oracle-token")
RAEG_ADMIN_DSN = os.environ.get("RAEG_RUN_ADMIN_DSN", "postgresql://postgres:postgres@127.0.0.1:55432/raeg")
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_HOST_ADDRESS", "127.0.0.1:17233")
RAEG_TABLES = ("executions", "submissions", "attempts", "evidence", "decisions", "execution_events", "anomalies")
PROFILES = ["--profile", "systems", "--profile", "temporal"]


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "DOCKER_CONTEXT": os.environ.get("DOCKER_CONTEXT", "colima")}
    return subprocess.run(["docker", "compose", *PROFILES, *args], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=600, check=check)


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = sorted({k for r in rows for k in r})
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v for k, v in r.items()})


async def collect_oracle(out: Path) -> dict[str, list]:
    async with httpx.AsyncClient(base_url=ORACLE_URL, headers={"x-oracle-token": ORACLE_TOKEN}, timeout=60) as c:
        ledger = (await c.get("/oracle/ledger")).json()
        receipts = (await c.get("/oracle/receipts")).json()
        faults = (await c.get("/oracle/faults")).json()
        keys = (await c.get("/oracle/keys")).json()
        config = (await c.get("/oracle/config")).json()
    _write_csv(out / "oracle_ledger.csv", ledger)
    _write_csv(out / "provider_receipts.csv", receipts)
    _write_csv(out / "fault_log.csv", faults)
    (out / "provider_keys.json").write_text(json.dumps(keys, indent=1, sort_keys=True), encoding="utf-8")
    (out / "provider_config.json").write_text(json.dumps(config, indent=1, sort_keys=True), encoding="utf-8")
    return {"ledger": ledger, "receipts": receipts, "faults": faults}


async def collect_raeg(out: Path) -> None:
    (out / "raeg").mkdir(exist_ok=True)
    conn = await asyncpg.connect(RAEG_ADMIN_DSN)
    try:
        for table in RAEG_TABLES:
            exists = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}")
            if exists:
                await conn.copy_from_query(f"SELECT * FROM {table} ORDER BY 1", output=str(out / "raeg" / f"{table}.csv"),
                                           format="csv", header=True)
    finally:
        await conn.close()


async def collect_temporal(out: Path, keys: list[str]) -> int:
    from temporalio.client import Client

    (out / "temporal" / "histories").mkdir(parents=True, exist_ok=True)
    client = await Client.connect(TEMPORAL_ADDRESS, namespace="default")
    n = 0
    for key in keys:
        try:
            history = await client.get_workflow_handle(key).fetch_history()
        except Exception as exc:  # no workflow (e.g. never started): record why
            (out / "temporal" / "histories" / f"{key}.missing.txt").write_text(repr(exc), encoding="utf-8")
            continue
        (out / "temporal" / "histories" / f"{key}.json").write_text(history.to_json(), encoding="utf-8")
        n += 1
    return n


def collect_exchange_logs(out: Path) -> None:
    dest = out / "exchange_logs"
    dest.mkdir(exist_ok=True)
    compose("cp", "systems:/exchange-logs/.", str(dest))


def collect_container_logs(out: Path) -> None:
    (out / "logs").mkdir(exist_ok=True)
    services = [s for s in compose("ps", "--services", "-a").stdout.split() if s]
    for svc in services:
        logs = compose("logs", "--no-color", "--timestamps", svc, check=False)
        (out / "logs" / f"{svc}.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
