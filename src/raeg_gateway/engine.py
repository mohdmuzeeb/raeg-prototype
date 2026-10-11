"""Recovery engine: drives the pure controller against the persistent store and the shared provider
client (DESIGN §4, §5.1). Every state change goes through GatewayStore with the controller's rule id,
the full decision input snapshot, and the ids of the evidence rows it relied on.

Evidence is always written *before* the decision that uses it. A worker that has lost its lease
(fenced) never changes state: it appends its response as `late_response` evidence, which the
controller later consumes through T15/T15a or an annotation/anomaly.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from raeg_common.models import Capability, Observation, PaymentPayload
from raeg_common.provider_client import DispatchResult, LookupResult, ProviderClient
from raeg_gateway import controller as ctl
from raeg_gateway.controller import Decision, LookupView, Policy, Snapshot
from raeg_gateway.store import GatewayStore, OwnerFence, ts_str
from raeg_gateway.transitions import TERMINAL, Basis, State


@dataclass
class StepOutcome:
    action_key: str
    state: State
    progressed: bool


def _parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class RecoveryEngine:
    def __init__(self, store: GatewayStore, client: ProviderClient, worker_id: str, system_id: str | None = None) -> None:
        self.store = store
        self.client = client
        self.worker_id = worker_id
        self.system_id = system_id  # background passes only touch this system's actions (None = all)

    # ------------------------------------------------------------------ entry points

    async def submit(self, action_key: str, payload: PaymentPayload, profile_id: str, capability: Capability,
                     policy: Policy) -> str:
        result = await self.store.submit(action_key, payload.model_dump(), payload.fingerprint(), profile_id,
                                         capability.model_dump(mode="json"), policy.to_json(), self.worker_id)
        return result.outcome

    async def run_to_quiescence(self, action_key: str, max_steps: int = 100, poll_s: float = 0.02) -> State:
        """Drive one action until it is terminal/escalated or no step is possible (e.g. waiting on time)."""
        state = State((await self.store.snapshot(action_key))["state"])
        for _ in range(max_steps):
            outcome = await self.step(action_key)
            state = outcome.state
            if state in TERMINAL or state == State.ESCALATED:
                await self.consume_late_evidence()
                return state
            if not outcome.progressed:
                await asyncio.sleep(poll_s)
                await self.sweep()
        return state

    async def sweep(self) -> int:
        return len([r for r in await self.store.sweep_expired(self.worker_id, system_id=self.system_id) if r.applied])

    async def run_once(self, limit: int = 50) -> int:
        """One background-worker pass: sweep, late evidence, due actions, duplicate audits."""
        n = await self.sweep()
        n += await self.consume_late_evidence()
        for key in await self.store.due_actions(limit, system_id=self.system_id):
            if (await self.step(key)).progressed:
                n += 1
        for key in await self.store.audit_candidates(limit, system_id=self.system_id):
            if (await self.step(key)).progressed:
                n += 1
        return n

    # ------------------------------------------------------------------ one step

    async def step(self, action_key: str) -> StepOutcome:
        row = await self.store.snapshot(action_key)
        state = State(row["state"])
        c = Capability.model_validate(row["capability"])
        p = Policy.from_json(row["policy"])
        now = await self.store.now()
        if state == State.PREPARED:
            return await self._dispatch(row, c, p, Basis.INITIAL)
        if state == State.RETRY_WAIT:
            if row["next_action_at"] and row["next_action_at"] > now:
                return StepOutcome(action_key, state, False)
            return await self._dispatch(row, c, p, Basis(row["retry_basis"]))
        if state == State.UNKNOWN:
            if row["next_action_at"] and row["next_action_at"] > now:
                return StepOutcome(action_key, state, False)
            if await self._late_success_for(row):
                return StepOutcome(action_key, State((await self.store.snapshot(action_key))["state"]), True)
            x = await self._snapshot(row)
            reached = await self._lookups_reached_horizon(x, c)
            decision = ctl.decide(x, c, p, now, lookups_reached_horizon=reached)
            return await self._apply_unknown(row, x, c, p, now, decision)
        if state == State.SUCCEEDED:
            progressed = await self._duplicate_audit(row, c, p, now)
            return StepOutcome(action_key, state, progressed)
        return StepOutcome(action_key, state, False)  # leased by someone else, or absorbing

    # ------------------------------------------------------------------ dispatch path (T2/T13 → T3/T4/T5)

    async def _dispatch(self, row: dict[str, Any], c: Capability, p: Policy, basis: Basis) -> StepOutcome:
        key = row["action_key"]
        guard = ctl.claim_guard(c, p) if basis != Basis.INITIAL else (lambda *_: (True, "CLAIM-INITIAL", "first attempt"))
        claim = await self.store.claim(key, worker_id=self.worker_id, attempt_ms=p.attempt_ms, basis=basis, guard=guard)
        if not claim.applied:
            return StepOutcome(key, State((await self.store.snapshot(key))["state"]), claim.reason == "GUARD_FAILED")
        claimed_mono = time.monotonic()
        db_now = await self.store.now()
        assert claim.deadline is not None and claim.attempt_no is not None
        budget_s = (claim.deadline - db_now).total_seconds() - (time.monotonic() - claimed_mono)
        payload = PaymentPayload.model_validate(row["payload"])
        idem_key = key if c.idempotency else None  # defence in depth: always send k when I

        if budget_s * 1000 < p.min_send_budget_ms:  # efficiency only; NOT_SENT stays ambiguous (decision 9)
            ev = await self.store.append_evidence(key, kind="transport_error", worker_id=self.worker_id,
                                                  attempt_no=claim.attempt_no, classification=Observation.AMBIGUOUS.value,
                                                  request={"skipped": "SEND_BUDGET_BELOW_MINIMUM", "budget_s": budget_s})
            result_obs, extra = Observation.AMBIGUOUS, {"error_class": "SEND_SKIPPED_LOW_BUDGET"}
            evidence_ids = [ev]
            response: DispatchResult | None = None
        else:
            response = await self.client.dispatch(
                key, payload, claim.attempt_no, claim.deadline if p.send_deadline else None, idem_key,
                timeout_s=max(budget_s - p.margin_ms / 1000, 0.05),
            )
            ev = await self._dispatch_evidence(key, claim.attempt_no, response, fenced=False)
            evidence_ids = [ev]
            result_obs = response.observation
            extra = {"provider_txn_id": response.provider_txn_id, "replayed": response.replayed,
                     "error_class": response.error_class}

        fresh = await self.store.snapshot(key)
        x = await self._snapshot(fresh)
        decision = ctl.on_dispatch_result(x, result_obs, c, p, **extra)
        result = await self.store.transition(
            key, decision.transition, worker_id=self.worker_id, rule_id=decision.rule_id,
            inputs=self._inputs(x, c, p, await self.store.now(), observation=result_obs.value, detail=decision.detail,
                                **{k: v for k, v in extra.items() if v is not None}),
            patch=decision.patch, evidence_ids=evidence_ids,
            fence=OwnerFence(self.worker_id, claim.lease_token, claim.attempt_no),
        )
        if not result.applied and result.reason == "FENCED" and response is not None:
            # Lost the lease while in flight (zombie / slow worker): keep the response as late evidence.
            await self._dispatch_evidence(key, claim.attempt_no, response, fenced=True)
        return StepOutcome(key, State((await self.store.snapshot(key))["state"]), True)

    async def _dispatch_evidence(self, key: str, attempt_no: int, r: DispatchResult, fenced: bool) -> int:
        raw = json.dumps(r.body).encode() if r.body is not None else None
        return await self.store.append_evidence(
            key, kind="late_response" if fenced else ("dispatch_response" if r.http_status else "transport_error"),
            worker_id=self.worker_id, attempt_no=attempt_no, sent_at=_parse_ts(r.sent_at), http_status=r.http_status,
            request={"error_class": r.error_class, "replayed": r.replayed, "latency_ms": r.latency_ms},
            response_headers=r.headers, raw_body=raw, classification=r.observation.value, fenced=fenced,
        )

    # ------------------------------------------------------------------ UNKNOWN path (T6/T12/T14/TW)

    async def _apply_unknown(self, row, x: Snapshot, c: Capability, p: Policy, now: datetime, d: Decision) -> StepOutcome:
        key = row["action_key"]
        inputs = self._inputs(x, c, p, now, detail=d.detail)
        if d.transition == "T6":
            res = await self.store.transition(key, "T6", worker_id=self.worker_id, rule_id=d.rule_id, inputs=inputs,
                                              patch=d.patch, expected_version=row["version"], lease_ms=p.verify_lease_ms)
            if not res.applied:
                return StepOutcome(key, State((await self.store.snapshot(key))["state"]), False)
            return await self._verify(key, c, p)
        res = await self.store.transition(key, d.transition, worker_id=self.worker_id, rule_id=d.rule_id, inputs=inputs,
                                          patch=d.patch, expected_version=row["version"])
        return StepOutcome(key, State((await self.store.snapshot(key))["state"]), res.applied and d.transition != "TW")

    # ------------------------------------------------------------------ VERIFYING path (T7–T11)

    async def _verify(self, key: str, c: Capability, p: Policy) -> StepOutcome:
        row = await self.store.snapshot(key)
        lease_token = row["lease_expires_at"]
        r: LookupResult = await self.client.lookup_by_reference(key, timeout_s=p.lookup_timeout_ms / 1000)
        ev = await self.store.append_evidence(
            key, kind="lookup_response", worker_id=self.worker_id, provider_as_of=_parse_ts(r.as_of),
            http_status=r.http_status, request={"error_class": r.error_class, "latency_ms": r.latency_ms},
            response_headers=r.headers, raw_body=json.dumps(r.body).encode() if r.body is not None else None,
            classification="LOOKUP_OK" if r.ok else "LOOKUP_ERROR",
        )
        row = await self.store.snapshot(key)
        x = await self._snapshot(row)
        now = await self.store.now()
        view = LookupView(r.ok, _parse_ts(r.as_of), tuple(r.records))
        d = ctl.on_lookup(x, view, c, p, now)
        res = await self.store.transition(
            key, d.transition, worker_id=self.worker_id, rule_id=d.rule_id,
            inputs=self._inputs(x, c, p, now, detail=d.detail, lookup={"ok": r.ok, "as_of": r.as_of,
                                                                       "n_records": len(r.records)}),
            patch=d.patch, evidence_ids=[ev], fence=OwnerFence(self.worker_id, lease_token),
        )
        if res.applied and d.anomaly:
            await self.store.append_anomaly(key, d.anomaly, [ev], {"rule_id": d.rule_id})
        return StepOutcome(key, State((await self.store.snapshot(key))["state"]), res.applied)

    # ------------------------------------------------------------------ late success (T15/T15a/TA/anomalies)

    async def consume_late_evidence(self) -> int:
        n = 0
        for ev in await self.store.unconsumed_late_evidence(system_id=self.system_id):
            row = await self.store.snapshot(ev["action_key"])
            if await self._consume_one(row, ev):
                n += 1
        return n

    async def _late_success_for(self, row: dict[str, Any]) -> bool:
        for ev in await self.store.unconsumed_late_evidence():
            if ev["action_key"] == row["action_key"] and await self._consume_one(row, ev):
                return True
        return False

    async def _consume_one(self, row: dict[str, Any], ev: dict[str, Any]) -> bool:
        key = row["action_key"]
        c = Capability.model_validate(row["capability"])
        p = Policy.from_json(row["policy"])
        x = await self._snapshot(row)
        now = await self.store.now()
        body = json.loads(ev["raw_body"]) if ev["raw_body"] else None
        if ev["classification"] != Observation.COMMITTED.value:
            await self.store.note_decision(key, worker_id=self.worker_id, rule_id="LS-NOT-SUCCESS",
                                           inputs=self._inputs(x, c, p, now, classification=ev["classification"]),
                                           evidence_ids=[ev["id"]], output={"noop": "late evidence is not a success"})
            return True
        d = ctl.late_success(x, ev["attempt_no"], body)
        if d is None:
            return False  # leased: defer
        inputs = self._inputs(x, c, p, now, detail=d.detail, late_evidence_id=ev["id"])
        if d.anomaly:
            await self.store.append_anomaly(key, d.anomaly, [ev["id"]], {"rule_id": d.rule_id, "detail": d.detail})
        if d.transition is None:
            await self.store.note_decision(key, worker_id=self.worker_id, rule_id=d.rule_id, inputs=inputs,
                                           evidence_ids=[ev["id"]], output={"noop": d.detail, "anomaly": d.anomaly})
            return True
        res = await self.store.transition(key, d.transition, worker_id=self.worker_id, rule_id=d.rule_id, inputs=inputs,
                                          patch=d.patch, evidence_ids=[ev["id"]], expected_version=row["version"])
        return res.applied

    # ------------------------------------------------------------------ duplicate audit (rule DS)

    async def _duplicate_audit(self, row: dict[str, Any], c: Capability, p: Policy, now: datetime) -> bool:
        key = row["action_key"]
        if row["duplicate_detected"] or not ctl.lookup_usable(c, p):
            return False
        x = await self._snapshot(row)
        single, why = ctl.single_processing(x, c, p)
        if single:
            return False
        if await self.store.evidence_rows(key, "duplicate_audit"):
            return False
        settle = ctl.settle_time(x, c)
        if settle is not None and now < settle:
            return False
        r = await self.client.lookup_by_reference(key, timeout_s=p.lookup_timeout_ms / 1000)
        ev = await self.store.append_evidence(
            key, kind="duplicate_audit", worker_id=self.worker_id, provider_as_of=_parse_ts(r.as_of),
            http_status=r.http_status, raw_body=json.dumps(r.body).encode() if r.body is not None else None,
            classification="LOOKUP_OK" if r.ok else "LOOKUP_ERROR",
        )
        succeeded = [rec for rec in r.records if rec.get("client_reference") == key and rec.get("status") == "succeeded"]
        inputs = self._inputs(x, c, p, now, detail=why, n_succeeded=len(succeeded), as_of=r.as_of)
        if len(succeeded) > 1:
            await self.store.append_anomaly(key, "DUPLICATE_DETECTED", [ev], {"n_succeeded": len(succeeded)})
            await self.store.transition(key, "TA", worker_id=self.worker_id, rule_id="DS-AUDIT-DUPLICATE", inputs=inputs,
                                        patch={"duplicate_detected": True}, evidence_ids=[ev], expected_version=row["version"])
        else:
            await self.store.note_decision(key, worker_id=self.worker_id, rule_id="DS-AUDIT-CLEAN", inputs=inputs,
                                           evidence_ids=[ev], output={"n_succeeded": len(succeeded), "ok": r.ok})
        return True

    # ------------------------------------------------------------------ helpers

    async def _snapshot(self, row: dict[str, Any]) -> Snapshot:
        return Snapshot.from_row(row, await self.store.attempts(row["action_key"]))

    async def _lookups_reached_horizon(self, x: Snapshot, c: Capability) -> bool:
        settle = ctl.settle_time(x, c)
        if settle is None:
            return False
        rows = await self.store.evidence_rows(x.action_key, "lookup_response")
        return any(r["provider_as_of"] is not None and r["provider_as_of"] >= settle for r in rows)

    @staticmethod
    def _inputs(x: Snapshot, c: Capability, p: Policy, now: datetime, **extra: Any) -> dict[str, Any]:
        """The complete input snapshot of a decision (DESIGN §5.2 `decisions.inputs`)."""
        return {"snapshot": x.to_json(), "capability": c.model_dump(mode="json"), "policy": p.to_json(),
                "now": ts_str(now), **extra}
