"""Run an experiment config (DESIGN §7, §8): for each cell × seed, generate the workload and fault plan
once, then run every system on fresh volumes, one at a time, in a seeded random order.

Each run writes an immutable results/raw/<run_id>/ with manifest.json, the workload, the fault plan,
all raw evidence, and audit.json. A failing run is kept and marked aborted; it is never deleted.

usage: CODE_SHA=$(git rev-parse HEAD) uv run python -m harness.runner config/experiments/<name>.yaml
       [--systems A,B] [--seeds 1,2] [--cells id1,id2]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from harness import collector, fault_plan, workload
from harness.audit_run import audit
from harness.driver import Driver, DriverConfig
from harness.manifest import build_manifest, write_manifest
from raeg_common.profiles import load_all
from systems.base import load_systems
from systems.raeg_system import policy_for

ROOT = Path(__file__).resolve().parents[2]
SYSTEMS_URL = os.environ.get("SYSTEMS_URL", "http://127.0.0.1:18080")
ORACLE_URL = collector.ORACLE_URL
ORACLE_TOKEN = collector.ORACLE_TOKEN


class Cell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: str
    fault: str
    fault_rate: float = Field(default=0.2, ge=0, le=1)
    assumption_set: str = "S"  # S | B | V:<id> (DESIGN §11)

    @property
    def id(self) -> str:
        return f"{self.profile}-{self.fault}-r{int(self.fault_rate * 100):03d}"


class Experiment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    purpose: str
    excluded_from_paper: bool = True
    seeds: list[int]
    workload: workload.WorkloadConfig
    driver: DriverConfig
    cells: list[Cell]
    systems: list[str]


def stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


async def wait_http(url: str, timeout_s: float = 240) -> None:
    async with httpx.AsyncClient(timeout=3) as c:
        for _ in range(int(timeout_s / 0.5)):
            try:
                if (await c.get(url)).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.5)
    raise RuntimeError(f"{url} not healthy after {timeout_s}s")


def bring_up(profile_id: str, system_id: str, temporal: bool) -> None:
    collector.compose("down", "-v", "--remove-orphans")
    env_profile = ["--profile", "systems"] + (["--profile", "temporal"] if temporal else [])
    env = {**os.environ, "DOCKER_CONTEXT": os.environ.get("DOCKER_CONTEXT", "colima"),
           "PROVIDER_PROFILE_ID": profile_id, "PROVIDER_ALLOW_RESET": "0", "SYSTEMS": system_id,
           "CODE_SHA": os.environ.get("CODE_SHA", "unknown")}
    import subprocess

    subprocess.run(["docker", "compose", *env_profile, "up", "-d", "--build", "--wait"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=900, check=True)


async def configure_provider(profile: dict, plan: fault_plan.FaultPlan) -> None:
    prof = {**profile, "violations": {**profile.get("violations", {}), **plan.violations}}
    async with httpx.AsyncClient(base_url=ORACLE_URL, headers={"x-oracle-token": ORACLE_TOKEN}, timeout=30) as c:
        r = await c.post("/admin/config", json={"profile": prof, "fault_plan": plan.provider_plan()})
        r.raise_for_status()


def arm_hooks(plan: fault_plan.FaultPlan) -> None:
    import subprocess

    data = json.dumps(plan.hook_plan())
    subprocess.run(["docker", "compose", *collector.PROFILES, "exec", "-T", "systems", "sh", "-c",
                    "rm -f /exchange-logs/hooks.json.fired && cat > /exchange-logs/hooks.json.tmp "
                    "&& mv /exchange-logs/hooks.json.tmp /exchange-logs/hooks.json"],
                   cwd=ROOT, input=data, text=True, capture_output=True, check=True,
                   env={**os.environ, "DOCKER_CONTEXT": os.environ.get("DOCKER_CONTEXT", "colima")})


async def drain_raeg(keys: list[str], timeout_s: float) -> None:
    import asyncpg

    conn = await asyncpg.connect(collector.RAEG_ADMIN_DSN)
    try:
        for _ in range(int(timeout_s / 0.5)):
            n = await conn.fetchval(
                "SELECT count(*) FROM executions WHERE action_key = ANY($1) AND state NOT IN "
                "('SUCCEEDED','DECLINED','NOT_EXECUTED','ESCALATED')", keys)
            if n == 0:
                return
            await asyncio.sleep(0.5)
    finally:
        await conn.close()


async def run_one(exp: Experiment, cell: Cell, seed: int, system_id: str, intents: list, plan: fault_plan.FaultPlan,
                  order: list[str]) -> Path:
    systems = load_systems()
    cfg = systems[system_id]
    profiles = load_all()
    prof = profiles[cell.profile]
    run_id = f"{stamp()}_{exp.name}_{cell.id}_{system_id}_s{seed}"
    out = ROOT / "results" / "raw" / run_id
    out.mkdir(parents=True, exist_ok=False)
    resolved = {"experiment": exp.model_dump(mode="json"), "cell": cell.model_dump(mode="json"), "cell_id": cell.id,
                "seed": seed, "system": system_id, "system_config": cfg.__dict__, "system_order_this_seed": order,
                "profile": prof.model_dump(mode="json"),
                "raeg_policy": policy_for(cfg).to_json() if cfg.kind == "raeg" else None}
    manifest = build_manifest("experiment", {"run_id": run_id, "status": "running", "resolved_config": resolved,
                                             "excluded_from_paper": exp.excluded_from_paper})
    write_manifest(out / "manifest.json", manifest)
    workload.write(out / "workload.jsonl", intents)
    plan.write(out / "fault_plan.json")
    t_start = time.monotonic()
    try:
        bring_up(cell.profile, system_id, temporal=cfg.kind == "temporal")
        await wait_http(f"{SYSTEMS_URL}/healthz")
        await configure_provider(prof.model_dump(mode="json"), plan)
        arm_hooks(plan)
        await Driver(SYSTEMS_URL, system_id, cell.profile, exp.driver, out).run(intents)
        keys = [i.action_key for i in intents]
        if cfg.kind == "raeg":
            await drain_raeg(keys, exp.driver.drain_s + 30)
        await asyncio.sleep(exp.driver.drain_s)  # provider-side stalls / zombie sends finish
        await collector.collect_oracle(out)
        collector.collect_exchange_logs(out)
        if cfg.kind == "raeg":
            await collector.collect_raeg(out)
        if cfg.kind == "temporal":
            await collector.collect_temporal(out, keys)
        collector.collect_container_logs(out)
        report = await audit(out, system_kind=cfg.kind, assumption_set=cell.assumption_set,
                             raeg_admin_dsn=collector.RAEG_ADMIN_DSN if cfg.kind == "raeg" else None,
                             policy=policy_for(cfg).to_json() if cfg.kind == "raeg" else None,
                             capability=prof.capability.model_dump(mode="json"))
        (out / "audit.json").write_text(json.dumps(report, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
        manifest["status"] = "complete"
        manifest["audit_status"] = report["status"]
        manifest["audit_failed_checks"] = report["failed_checks"]
    except Exception as exc:
        manifest["status"] = f"aborted:{type(exc).__name__}"
        (out / "ABORTED.txt").write_text(traceback.format_exc(), encoding="utf-8")
        try:
            collector.collect_container_logs(out)
        except Exception:
            pass
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["wall_clock_s"] = round(time.monotonic() - t_start, 1)
    write_manifest(out / "manifest.json", manifest)
    print(f"[runner] {run_id}: {manifest['status']} audit={manifest.get('audit_status')} "
          f"failed={manifest.get('audit_failed_checks')} wall={manifest['wall_clock_s']}s", flush=True)
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--systems")
    ap.add_argument("--seeds")
    ap.add_argument("--cells")
    args = ap.parse_args()
    exp = Experiment.model_validate(yaml.safe_load(Path(args.config).read_text()))
    systems = args.systems.split(",") if args.systems else exp.systems
    seeds = [int(s) for s in args.seeds.split(",")] if args.seeds else exp.seeds
    registry = load_systems()
    profiles = load_all()
    for cell in exp.cells:
        if args.cells and cell.id not in args.cells.split(","):
            continue
        for seed in seeds:
            namespace = f"{exp.name}-{cell.id}-s{seed}"
            intents = workload.generate(seed, exp.workload, namespace)
            any_cfg = registry[systems[0]]
            cap = profiles[cell.profile].capability
            timing = fault_plan.Timing(any_cfg.attempt_ms, any_cfg.margin_ms, cap.lookup_lag_ms, cap.key_ttl_ms)
            plan = fault_plan.generate(seed, intents, cell.fault, cell.fault_rate, timing, plan_id=namespace)
            order = list(systems)
            random.Random(f"order|{seed}|{namespace}").shuffle(order)
            for system_id in order:
                await run_one(exp, cell, seed, system_id, intents, plan, order)
    collector.compose("down", "-v", "--remove-orphans", check=False)


if __name__ == "__main__":
    asyncio.run(main())
