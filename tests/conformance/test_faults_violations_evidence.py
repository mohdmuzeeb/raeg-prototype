"""Provider-side fault injection (F1, F3, F4, F9, declines), V1–V4 switches, and evidence integrity."""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from .conftest import ms, profile, ts


def rule(ref: str, idx: int, fault: str, **params) -> dict:
    return {"client_reference": ref, "ingress_index": idx, "fault": fault, "params": params}


# ----------------------------------------------------------------------------- faults


async def test_f1_lost_response_reset_after_commit(provider) -> None:
    await provider.configure(profile("P1"), [rule("f1", 1, "LOST_RESPONSE", mode="reset")])
    r = await provider.pay("f1", key="f1")
    assert r.status is None and r.error is not None, f"client must not get a usable response, got {r}"
    assert len(await provider.ledger("f1")) == 1, "the effect is committed"
    (receipt,) = await provider.receipts("f1")
    assert receipt["disposition"] == "committed" and receipt["detail"] == {"fault": "LOST_RESPONSE"}
    assert [f["fault"] for f in await provider.faults()] == ["LOST_RESPONSE"]


async def test_f1_lost_response_blackhole(provider) -> None:
    await provider.configure(profile("P1"), [rule("f1b", 1, "LOST_RESPONSE", mode="blackhole", blackhole_ms=1500)])
    r = await provider.pay("f1b", timeout=0.5)
    assert r.error == "ReadTimeout"
    await asyncio.sleep(0.2)
    assert len(await provider.ledger("f1b")) == 1


async def test_f3_pre_commit_stall_never_commits(provider) -> None:
    await provider.configure(profile("P1"), [rule("f3", 1, "PRE_COMMIT_STALL", stall_ms=1000)])
    r = await provider.pay("f3", key="f3", timeout=0.4)
    assert r.error == "ReadTimeout"
    await asyncio.sleep(1.0)
    assert await provider.ledger("f3", status=None) == []
    (receipt,) = await provider.receipts("f3")
    assert receipt["disposition"] == "stalled_no_commit"


async def test_f4_delayed_commit_after_client_timeout(provider) -> None:
    await provider.configure(profile("P1"), [rule("f4", 1, "DELAYED_COMMIT", delay_ms=800)])
    r = await provider.pay("f4", timeout=0.3)
    assert r.error == "ReadTimeout"
    assert await provider.ledger("f4") == [], "not yet committed when the client gave up"
    await asyncio.sleep(1.0)
    assert len(await provider.ledger("f4")) == 1


async def test_f9_ingress_hold_delays_arrival(provider) -> None:
    await provider.configure(profile("P1"), [rule("f9", 1, "INGRESS_HOLD", hold_ms=700)])
    sent = await provider.now()
    assert (await provider.pay("f9")).status == 201
    (receipt,) = await provider.receipts("f9")
    assert receipt["held_ms"] == 700
    assert ts(receipt["arrived_at"]) - sent >= ms(700)


async def test_insufficient_funds_decline_after_ambiguous_commit(provider) -> None:
    """Why rule DD exists (DESIGN §2.8): without I, attempt 2 is declined *because* attempt 1 committed."""
    await provider.configure(profile("P4"), [rule("dd", 1, "LOST_RESPONSE")])
    await provider.admin.post("/admin/accounts", json=[{"payer_id": "payer-dd", "balance_minor": 1500}])
    first = await provider.pay("dd", payer="payer-dd")
    second = await provider.pay("dd", payer="payer-dd")
    assert first.status is None  # ambiguous to the client
    assert second.status == 402 and second.json["decline_code"] == "insufficient_funds"
    assert len(await provider.ledger("dd")) == 1, "E = 1 although the latest response is a decline"


async def test_deterministic_decline_rate(provider) -> None:
    await provider.configure(profile("P1", behavior={"decline_rate": 1.0}))
    r1 = await provider.pay("dr1")
    r2 = await provider.pay("dr1")
    assert r1.status == r2.status == 402 and r1.json["decline_code"] == "card_declined"


# ----------------------------------------------------------------------------- violations V1–V4


async def test_v1_lookup_lag_exceeding_delta_breaks_l2(provider) -> None:
    prof = profile("P1", violations={"lookup_lag_exceeds_declared_ms": 1500})
    await provider.configure(prof)
    await provider.pay("v1")
    (payment,) = await provider.ledger("v1")
    await provider.sleep_until(ts(payment["committed_at"]) + ms(prof.capability.lookup_lag_ms + 100))
    resp = await provider.lookup("v1")
    assert ts(resp.json()["as_of"]) >= ts(payment["committed_at"]) + ms(prof.capability.lookup_lag_ms)
    assert resp.json()["records"] == [], "V1 active: committed payment invisible after declared δ"


async def test_v2_declared_deadline_not_enforced(provider) -> None:
    prof = profile("P1", violations={"ignore_deadline": True})
    await provider.configure(prof, [rule("v2", 1, "DELAYED_COMMIT", delay_ms=800)])
    assert prof.capability.deadline_enforced
    r = await provider.pay("v2", deadline=await provider.now() + ms(300))
    assert r.status == 201
    (payment,) = await provider.ledger("v2")
    assert ts(payment["committed_at"]) > ts(payment["deadline"]), "V2 active: commit after deadline"


async def test_v3_retention_shorter_than_declared(provider) -> None:
    prof = profile("P2", violations={"actual_retention_ms": 1000})
    await provider.configure(prof)
    await provider.pay("v3", key="v3")
    created = await provider.key_created_at("v3")
    await provider.sleep_until(created + ms(1300))  # < declared τ (3000), > actual retention
    r = await provider.pay("v3", key="v3")
    assert not r.replayed
    assert len(await provider.ledger("v3")) == 2, "V3 active: duplicate within declared τ"


async def test_v4_clock_offset(provider, provider_db) -> None:
    await provider.configure(profile("P1", violations={"clock_offset_ms": 5000}))
    skew = await provider_db.fetchval("SELECT extract(epoch FROM provider_now() - clock_timestamp()) * 1000")
    assert 4900 < skew < 5100
    await provider.configure(profile("P1"))
    skew = await provider_db.fetchval("SELECT extract(epoch FROM provider_now() - clock_timestamp()) * 1000")
    assert abs(skew) < 100


# ----------------------------------------------------------------------------- evidence integrity


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE payments SET amount_minor = 1",
        "DELETE FROM payments",
        "UPDATE receipts SET held_ms = 1",
        "DELETE FROM receipt_outcomes",
        "DELETE FROM key_events",
        "DELETE FROM fault_log",
        "UPDATE config_log SET config = '{}'",
    ],
)
async def test_evidence_tables_are_append_only(provider, provider_db, statement: str) -> None:
    await provider.configure(profile("P1"), [rule("ev", 1, "FORCE_DECLINE")])
    await provider.pay("ev", key="ev")
    with pytest.raises(asyncpg.PostgresError, match="append-only") as err:
        await provider_db.execute(statement)
    assert err.value.sqlstate == "RA002"


async def test_oracle_requires_token(provider) -> None:
    resp = await provider.http.get("/oracle/ledger")
    assert resp.status_code == 404, "oracle routes are not served on the public port"
    import httpx

    from .conftest import ORACLE_URL

    async with httpx.AsyncClient(base_url=ORACLE_URL) as anon:
        assert (await anon.get("/oracle/ledger")).status_code == 403


async def test_ledger_and_receipt_log_cross_check(provider) -> None:
    """Evaluation validity (DESIGN §11): every committed receipt ↔ exactly one ledger row."""
    rules = [
        rule("x", 1, "LOST_RESPONSE"),
        rule("x", 2, "ERROR_5XX_PRE"),
        rule("y", 1, "FORCE_DECLINE"),
        rule("z", 1, "PRE_COMMIT_STALL", stall_ms=100),
    ]
    await provider.configure(profile("P4"), rules)
    for ref in ("x", "x", "x", "y", "z", "z"):
        await provider.pay(ref)
    ledger = await provider.ledger(status=None)
    receipts = await provider.receipts()
    by_receipt = {r["receipt_id"]: r for r in receipts}
    assert all(r["disposition"] is not None for r in receipts), "every receipt has a final disposition"
    committed = {r["receipt_id"] for r in receipts if r["disposition"] in ("committed", "declined")}
    assert committed == {p["receipt_id"] for p in ledger}
    for p in ledger:
        assert by_receipt[p["receipt_id"]]["payment_id"] == p["payment_id"]
    assert {r: len([p for p in ledger if p["client_reference"] == r and p["status"] == "succeeded"]) for r in "xyz"} == {
        "x": 2,
        "y": 0,
        "z": 1,
    }
