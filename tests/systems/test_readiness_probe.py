"""N60: the end-to-end readiness probe used by the harness before the driver starts."""

from __future__ import annotations

import httpx
import pytest

from tests.systems.conftest import REGISTRY, SYSTEMS_URL

pytestmark = pytest.mark.systems


@pytest.mark.parametrize("system_id", sorted(REGISTRY))
async def test_ready_endpoint(system_id: str, provider) -> None:
    async with httpx.AsyncClient(base_url=SYSTEMS_URL, timeout=30) as c:
        r = await c.get(f"/admin/ready/{system_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ready"] is True
    if REGISTRY[system_id].kind == "temporal":
        assert body["probe"] == "temporal_workflow" and body["probe_ms"] > 0
    else:
        assert body["probe"] is None


async def test_probe_never_contacts_provider(provider) -> None:
    """The probe workflow must leave no trace at the provider (it would otherwise pollute the oracle)."""
    before = len((await provider.admin.get("/oracle/receipts")).json())
    async with httpx.AsyncClient(base_url=SYSTEMS_URL, timeout=30) as c:
        for _ in range(3):
            assert (await c.get("/admin/ready/B3")).status_code == 200
    after = len((await provider.admin.get("/oracle/receipts")).json())
    assert after == before


async def test_unknown_system_is_404() -> None:
    async with httpx.AsyncClient(base_url=SYSTEMS_URL, timeout=30) as c:
        assert (await c.get("/admin/ready/NOPE")).status_code == 404
