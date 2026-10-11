"""F0 smoke test for every registered system, plus transport parity at the provider."""

from __future__ import annotations

import pytest

from analysis.claims import normalize
from raeg_common.models import Claim

from .conftest import REGISTRY, configure, new_key, submit


def sends_key(system_id: str) -> bool:
    cfg = REGISTRY[system_id]
    return cfg.kind in ("temporal", "raeg") or cfg.idempotency_key  # P1 has I, so RAEG sends k


@pytest.mark.parametrize("system_id", sorted(REGISTRY))
async def test_f0_every_system(systems, provider, system_id: str) -> None:
    key = new_key(system_id)
    await configure(provider, "P1", key)
    native = await submit(systems, system_id, key, "P1")
    assert native["status"] == "ok" and native["system"] == system_id and native["action_key"] == key
    assert normalize(native) == Claim.SUCCESS
    assert len(await provider.ledger(key)) == 1
    (receipt,) = await provider.receipts(key)
    assert receipt["system_hdr"] == system_id, "every call goes through the shared client with its system id"
    assert receipt["attempt_hdr"] == "1"
    assert (receipt["deadline"] is not None) == REGISTRY[system_id].send_deadline, "/noDL controls send no deadline"
    assert (receipt["idempotency_key"] == key) == sends_key(system_id)
    if receipt["deadline"] is not None:
        assert receipt["headers"]["request-deadline"]
    status = await systems.get(f"/s/{system_id}/v1/actions/{key}")
    assert status.json()["status"] == "ok"


async def test_unknown_system_is_404(systems) -> None:
    r = await systems.post("/s/NOPE/v1/actions", json={"action_key": "x", "payload": {}, "profile_id": "P1"})
    assert r.status_code in (404, 422)


def test_registry_timing_is_identical_across_systems() -> None:
    fields = ("attempt_ms", "margin_ms", "client_timeout_ms", "max_dispatch", "backoff_base_ms", "max_backoff_ms")
    assert len({tuple(getattr(c, f) for f in fields) for c in REGISTRY.values()}) == 1


def test_systems_use_only_the_shared_client() -> None:
    """No system module may talk HTTP to the provider directly (DESIGN §5.4 transport parity)."""
    import ast
    from pathlib import Path

    for path in Path("src/systems").rglob("*.py"):
        tree = ast.parse(path.read_text())
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not imported & {"httpx", "requests", "aiohttp", "urllib"}, path
