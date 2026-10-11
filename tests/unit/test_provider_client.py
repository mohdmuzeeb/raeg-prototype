from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from raeg_common.exchange_log import ExchangeLog
from raeg_common.hooks import HookAction, HookPlan, HookPoint
from raeg_common.models import Observation, PaymentPayload
from raeg_common.provider_client import ProviderClient, classify

PAYLOAD = PaymentPayload(amount_minor=500, currency="USD", payer_id="payer", payee_id="payee")
OK_BODY = {"status": "succeeded", "provider_txn_id": "pay_1", "client_reference": "k1"}


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (201, OK_BODY, Observation.COMMITTED),
        (200, OK_BODY, Observation.COMMITTED),
        (201, {"status": "weird"}, Observation.AMBIGUOUS),
        (402, {"status": "declined"}, Observation.DECLINED),
        (422, {"error": {"code": "VALIDATION_ERROR"}}, Observation.DECLINED),
        (422, {"error": {"code": "IDEMPOTENCY_MISMATCH"}}, Observation.INTERNAL),
        (409, {"error": {"code": "DEADLINE_EXCEEDED"}}, Observation.DEADLINE_REJECTED),
        (409, {"error": {"code": "IDEMPOTENCY_IN_PROGRESS"}}, Observation.AMBIGUOUS),
        (500, {}, Observation.AMBIGUOUS),
        (503, {"_raw_text": "x"}, Observation.AMBIGUOUS),
        (404, {}, Observation.AMBIGUOUS),
    ],
)
def test_classification_table(status: int, body: dict, expected: Observation) -> None:
    assert classify(status, body) is expected


class Recorder:
    def __init__(self, responder) -> None:
        self.requests: list[httpx.Request] = []
        self.responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)


def make_client(tmp_path: Path, responder, hooks: HookPlan | None = None, send_deadline: bool = True):
    rec = Recorder(responder)
    log_path = tmp_path / "exchanges.jsonl"
    client = ProviderClient(
        "http://provider",
        "test-system",
        timeout_s=1.0,
        hooks=hooks,
        log=ExchangeLog(str(log_path)),
        send_deadline=send_deadline,
        transport=httpx.MockTransport(rec),
    )
    return client, rec, log_path


def read_log(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


async def test_dispatch_sets_headers_and_logs(tmp_path: Path) -> None:
    client, rec, log_path = make_client(tmp_path, lambda r: httpx.Response(201, json=OK_BODY, headers={"Idempotent-Replayed": "true"}))
    deadline = datetime.now(UTC) + timedelta(seconds=2)
    result = await client.dispatch("k1", PAYLOAD, 3, deadline, idempotency_key="k1")
    await client.aclose()
    req = rec.requests[0]
    assert req.headers["Idempotency-Key"] == "k1"
    assert req.headers["X-RAEG-Attempt"] == "3"
    assert req.headers["X-System"] == "test-system"
    assert datetime.fromisoformat(req.headers["Request-Deadline"]) == deadline.replace(microsecond=deadline.microsecond // 1000 * 1000)
    assert json.loads(req.content) == {"client_reference": "k1", **PAYLOAD.model_dump()}
    assert result.observation is Observation.COMMITTED and result.replayed and result.provider_txn_id == "pay_1"
    (entry,) = read_log(log_path)
    assert entry["event"] == "exchange" and entry["observation"] == "COMMITTED" and entry["response"]["status"] == 201


async def test_nodl_control_omits_deadline_and_no_key_when_none(tmp_path: Path) -> None:
    client, rec, _ = make_client(tmp_path, lambda r: httpx.Response(201, json=OK_BODY), send_deadline=False)
    await client.dispatch("k1", PAYLOAD, 1, datetime.now(UTC), idempotency_key=None)
    await client.aclose()
    assert "Request-Deadline" not in rec.requests[0].headers
    assert "Idempotency-Key" not in rec.requests[0].headers


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("t"), httpx.RemoteProtocolError("reset"), httpx.ConnectError("refused")])
async def test_transport_failures_are_ambiguous(tmp_path: Path, exc: Exception) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise exc

    client, _, log_path = make_client(tmp_path, boom)
    result = await client.dispatch("k1", PAYLOAD, 1, None, None)
    await client.aclose()
    # NOT_SENT optimisation is disabled (decision 9): even ConnectError is AMBIGUOUS.
    assert result.observation is Observation.AMBIGUOUS
    assert result.error_class == type(exc).__name__
    assert read_log(log_path)[0]["error_class"] == type(exc).__name__


class Crashed(Exception):
    pass


def crash() -> None:
    raise Crashed()


@pytest.mark.parametrize("point", [HookPoint.CP1, HookPoint.CP3])
async def test_crash_hooks_fire_at_their_point(tmp_path: Path, point: HookPoint) -> None:
    hooks = HookPlan({("k1", 1): HookAction(point)}, exit_fn=crash)
    client, rec, log_path = make_client(tmp_path, lambda r: httpx.Response(201, json=OK_BODY), hooks=hooks)
    with pytest.raises(Crashed):
        await client.dispatch("k1", PAYLOAD, 1, None, None)
    await client.aclose()
    events = [e["event"] for e in read_log(log_path)]
    if point is HookPoint.CP1:
        assert rec.requests == [] and events == ["hook_fired"]
    else:
        assert len(rec.requests) == 1 and events == ["exchange", "hook_fired"]


async def test_hooks_only_fire_for_matching_attempt(tmp_path: Path) -> None:
    hooks = HookPlan({("k1", 2): HookAction(HookPoint.CP1)}, exit_fn=crash)
    client, rec, _ = make_client(tmp_path, lambda r: httpx.Response(201, json=OK_BODY), hooks=hooks)
    assert (await client.dispatch("k1", PAYLOAD, 1, None, None)).observation is Observation.COMMITTED
    with pytest.raises(Crashed):
        await client.dispatch("k1", PAYLOAD, 2, None, None)
    await client.aclose()
    assert len(rec.requests) == 1


async def test_pause_hook_delays_send(tmp_path: Path) -> None:
    hooks = HookPlan({("k1", 1): HookAction(HookPoint.PAUSE, pause_ms=150)})
    client, rec, log_path = make_client(tmp_path, lambda r: httpx.Response(201, json=OK_BODY), hooks=hooks)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await client.dispatch("k1", PAYLOAD, 1, None, None)
    await client.aclose()
    assert loop.time() - t0 >= 0.15
    assert [e["event"] for e in read_log(log_path)] == ["pause_begin", "pause_end", "exchange"]


async def test_cp2_crashes_while_request_in_flight(tmp_path: Path) -> None:
    class SlowTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.started = False

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.started = True
            await asyncio.sleep(0.5)
            return httpx.Response(201, json=OK_BODY)

    transport = SlowTransport()
    hooks = HookPlan({("k1", 1): HookAction(HookPoint.CP2, after_send_ms=50)}, exit_fn=crash)
    client = ProviderClient("http://p", "s", 1.0, hooks=hooks, log=ExchangeLog(str(tmp_path / "x.jsonl")), transport=transport)
    with pytest.raises(Crashed):
        await client.dispatch("k1", PAYLOAD, 1, None, None)
    await client.aclose()
    assert transport.started
    (entry,) = read_log(tmp_path / "x.jsonl")
    assert entry["event"] == "hook_fired" and entry["point"] == "CP2" and "note" not in entry


async def test_lookup_parsing(tmp_path: Path) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/payments":
            return httpx.Response(200, json={"as_of": "2026-01-01T00:00:00+00:00", "records": [OK_BODY]})
        return httpx.Response(501, json={"error": {"code": "NOT_SUPPORTED"}})

    client, _, _ = make_client(tmp_path, responder)
    ok = await client.lookup_by_reference("k1")
    bad = await client.lookup_by_provider_id("pay_1", "k1")
    await client.aclose()
    assert ok.ok and ok.as_of and ok.records == [OK_BODY]
    assert not bad.ok and bad.error_class == "HTTP_501"
