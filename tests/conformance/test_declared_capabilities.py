"""Every shipped profile behaves as its declared (I, τ, Q, δ, D) says (DESIGN §2.3, §5.3)."""

from __future__ import annotations

import pytest

from raeg_common.models import LookupMode

from .conftest import PROFILES, ms, profile

ALL = sorted(PROFILES)


@pytest.mark.parametrize("pid", ALL)
async def test_commit_is_recorded_in_ledger_and_receipt_log(provider, pid: str) -> None:
    await provider.configure(profile(pid))
    r = await provider.pay("c1", extra_headers={"X-RAEG-Attempt": "1"})
    assert r.status == 201 and r.json["status"] == "succeeded"
    (row,) = await provider.ledger("c1")
    assert row["provider_txn_id"] == r.json["provider_txn_id"]
    (receipt,) = await provider.receipts("c1")
    assert receipt["disposition"] == "committed" and receipt["payment_id"] == row["payment_id"]
    assert receipt["attempt_hdr"] == "1" and receipt["system_hdr"] == "conformance"
    # C-2 support: the response echoes every payload field
    for field in ("client_reference", "amount_minor", "currency", "payer_id", "payee_id", "memo"):
        assert field in r.json


@pytest.mark.parametrize("pid", ALL)
async def test_idempotency_matches_declaration(provider, pid: str) -> None:
    await provider.configure(profile(pid))
    first = await provider.pay("c2", key="c2")
    second = await provider.pay("c2", key="c2")
    assert first.status == 201
    if PROFILES[pid].capability.idempotency:
        assert second.status == 201 and second.replayed
        assert second.json["provider_txn_id"] == first.json["provider_txn_id"]
        assert len(await provider.ledger("c2")) == 1
    else:  # the header is ignored: a second effect
        assert second.status == 201 and not second.replayed
        assert len(await provider.ledger("c2")) == 2


@pytest.mark.parametrize("pid", ALL)
async def test_lookup_modes_match_declaration(provider, pid: str) -> None:
    prof = profile(pid)
    await provider.configure(prof)
    r = await provider.pay("c3")
    if prof.capability.lookup_lag_ms is not None:
        await provider.sleep_until(await provider.now() + ms(prof.capability.lookup_lag_ms + 50))
    by_ref = await provider.lookup("c3")
    by_id = await provider.lookup_id(r.json["provider_txn_id"])
    if prof.capability.lookup == LookupMode.BY_CLIENT_REF:
        assert by_ref.status_code == 200 and len(by_ref.json()["records"]) == 1
    else:
        assert by_ref.status_code == 501
    if prof.capability.lookup == LookupMode.NONE:
        assert by_id.status_code == 501
    else:
        assert by_id.status_code == 200 and by_id.json()["record"]["client_reference"] == "c3"


@pytest.mark.parametrize("pid", ALL)
async def test_deadline_at_receipt_matches_declaration(provider, pid: str) -> None:
    prof = profile(pid)
    await provider.configure(prof)
    past = await provider.now() - ms(1000)
    r = await provider.pay("c4", key="c4", deadline=past)
    if prof.capability.deadline_enforced:
        assert r.status == 409 and r.code == "DEADLINE_EXCEEDED"
        assert await provider.ledger("c4", status=None) == []
        (receipt,) = await provider.receipts("c4")
        assert receipt["disposition"] == "deadline_rejected_at_receipt"
        assert not [k for k in (await provider.keys())["keys"] if k["key"] == "c4"], "no reservation before receipt check"
    else:
        assert r.status == 201
        assert len(await provider.ledger("c4")) == 1


async def test_invalid_deadline_header_is_rejected_without_effect(provider) -> None:
    await provider.configure(profile("P1"))
    r = await provider.pay("c5", extra_headers={"Request-Deadline": "tomorrow"})
    assert r.status == 422 and r.code == "INVALID_DEADLINE"
    assert await provider.ledger("c5", status=None) == []


async def test_invalid_body_is_rejected_without_effect(provider) -> None:
    await provider.configure(profile("P1"))
    resp = await provider.http.post("/payments", json={"client_reference": "c6", "amount_minor": 1.5, "currency": "USD"})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert await provider.ledger("c6", status=None) == []
