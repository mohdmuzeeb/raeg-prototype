"""Fixtures for M5: every system is exercised only through its HTTP contract (DESIGN §5.4).

Requires `make up-systems`. These tests check that each system behaves as its *definition* says;
they are not measurements and produce no comparative results.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from systems.base import load_systems
from tests.conformance.conftest import Provider, profile

ROOT = Path(__file__).resolve().parents[2]
SYSTEMS_URL = os.environ.get("SYSTEMS_URL", "http://127.0.0.1:18080")
REGISTRY = load_systems()
PAYLOAD = {"amount_minor": 1000, "currency": "USD", "payer_id": "payer-1", "payee_id": "merchant-1", "memo": ""}
pytestmark = pytest.mark.systems


def compose(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "DOCKER_CONTEXT": os.environ.get("DOCKER_CONTEXT", "colima")}
    return subprocess.run(["docker", "compose", "--profile", "systems", "--profile", "temporal", *args], cwd=ROOT, env=env, input=stdin,
                          capture_output=True, text=True, timeout=180)


@pytest.fixture
async def provider():
    p = Provider()
    yield p
    await p.aclose()


@pytest.fixture
async def systems():
    async with httpx.AsyncClient(base_url=SYSTEMS_URL, timeout=90) as client:
        try:
            r = await client.get("/healthz")
        except httpx.TransportError as exc:
            pytest.fail(f"systems service not reachable — run `make up-systems` ({exc})")
        assert set(r.json()["systems"]) == set(REGISTRY)
        yield client


def new_key(system_id: str) -> str:
    return f"m5-{system_id}-{uuid.uuid4().hex[:8]}".replace("_", "-")


async def configure(provider: Provider, pid: str, key: str, rules: list[dict] | None = None, **overrides) -> None:
    await provider.configure(profile(pid, **overrides), [{**r, "client_reference": key} for r in (rules or [])])


async def submit(systems: httpx.AsyncClient, system_id: str, key: str, pid: str, wait_s: float = 60.0) -> dict:
    r = await systems.post(f"/s/{system_id}/v1/actions",
                           json={"action_key": key, "payload": PAYLOAD, "profile_id": pid, "wait_s": wait_s})
    r.raise_for_status()
    return r.json()


def rule(idx: int, fault: str, kind: str = "dispatch", **params) -> dict:
    return {"kind": kind, "ingress_index": idx, "fault": fault, "params": params}


def ts(v: str) -> datetime:
    return datetime.fromisoformat(v)


async def wait_healthy(service: str, timeout_s: float = 90) -> None:
    for _ in range(int(timeout_s / 0.5)):
        out = compose("ps", "--format", "json", service).stdout
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        if rows and all(r.get("State") == "running" and r.get("Health", "healthy") in ("healthy", "") for r in rows):
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"{service} did not become healthy")


def restart_count(service: str) -> int:
    cid = compose("ps", "-q", service).stdout.strip()
    out = subprocess.run(["docker", "inspect", "--format", "{{.RestartCount}}", cid], capture_output=True, text=True,
                         env={**os.environ, "DOCKER_CONTEXT": os.environ.get("DOCKER_CONTEXT", "colima")})
    return int(out.stdout.strip() or 0)


async def wait_restarted(service: str, before: int, url: str | None = None, timeout_s: float = 90) -> None:
    """Wait until Docker has actually restarted the service (RestartCount increased) and, if given, until
    its health endpoint answers. `compose ps` alone can still show the dying container as healthy."""
    for _ in range(int(timeout_s / 0.5)):
        if restart_count(service) > before:
            break
        await asyncio.sleep(0.5)
    else:
        raise AssertionError(f"{service} was not restarted")
    if url:
        async with httpx.AsyncClient(timeout=2) as client:
            for _ in range(int(timeout_s / 0.5)):
                try:
                    if (await client.get(url)).status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.5)
        raise AssertionError(f"{service} health endpoint did not come back")


def arm_hooks(hooks: list[dict]) -> None:
    compose("exec", "-T", "systems", "sh", "-c", "rm -f /exchange-logs/hooks.json.fired && cat > /exchange-logs/hooks.json.tmp && mv /exchange-logs/hooks.json.tmp /exchange-logs/hooks.json",
            stdin=json.dumps({"hooks": hooks}))


def disarm_hooks() -> None:
    compose("exec", "-T", "systems", "rm", "-f", "/exchange-logs/hooks.json", "/exchange-logs/hooks.json.fired")


def exchange_log(system_id: str, worker: bool = False) -> list[dict]:
    name = f"/exchange-logs/client_exchanges-{system_id}{'-worker' if worker else ''}.jsonl"
    out = compose("exec", "-T", "systems", "sh", "-c", f"cat {name} 2>/dev/null || true").stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]
