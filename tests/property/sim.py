"""Discrete-event model of one action under the RAEG controller, against an adversarial provider.

The provider model honours the *declared* contracts (K1–K7 incl. retention, L1–L5 with lag ≤ δ,
commit-time D, clock skew ≤ ε) unless a violation switch is set. Everything else — delivery delays,
processing times, lost responses, zombie pauses, lookup errors — is chosen by Hypothesis.

The engine logic (claim → send → classify → transition; verify; late evidence) mirrors
raeg_gateway.engine and uses the real pure controller functions. Time is integer milliseconds.
This is a logic check of the policy, not a performance model: it produces no benchmark numbers.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from hypothesis import strategies as st

from raeg_common.fingerprint import fingerprint
from raeg_common.models import Capability, LookupMode, Observation
from raeg_gateway import controller as ctl
from raeg_gateway.controller import AttemptView, LookupView, Policy, Snapshot
from raeg_gateway.transitions import TERMINAL, Basis, State

T0 = datetime(2026, 1, 1, tzinfo=UTC)
KEY = "k-sim"
PAYLOAD = {"amount_minor": 1000, "currency": "USD", "payer_id": "p", "payee_id": "m", "memo": ""}
FP = fingerprint(PAYLOAD)


def dt(t: int) -> datetime:
    return T0 + timedelta(milliseconds=t)


def to_ms(d: datetime) -> int:
    return (d - T0) // timedelta(milliseconds=1)


@dataclass
class Violations:
    lookup_lag_factor: int = 1  # V1: lag drawn up to factor·δ
    ignore_deadline: bool = False  # V2
    retention_ms: int | None = None  # V3: actual retention < declared τ
    skew_factor: int = 1  # V4: offset drawn up to factor·ε
    b1: bool = True  # Tier B assumption: honoured by draws when True (only relevant in BOUNDED mode)
    b1_arrival_only: bool = False  # B1 as literally worded in DESIGN v0.3: bounds arrival, not commit


@dataclass
class Worker:
    attempt_no: int
    deadline: int
    waiting_until: int
    done: bool = False


@dataclass
class Sim:
    c: Capability
    p: Policy
    data: Any
    v: Violations = field(default_factory=Violations)
    t: int = 0
    log: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.q: list = []
        self.seq = itertools.count()
        self.x: dict[str, Any] = dict(state=State.PREPARED, attempt_count=0, verify_attempts=0, segment_no=0,
                                      anchor=None, max_deadline=None, absence_proven_through=None, retry_basis=None,
                                      provider_txn_id=None, escalation_reason=None, next_action_at=None, lease=None)
        self.attempts: list[AttemptView] = []
        self.workers: dict[int, Worker] = {}
        self.late: list[tuple[int, dict]] = []
        self.anomalies: list[str] = []
        eps = self.p.clock_skew_ms * self.v.skew_factor
        self.off = self.draw(st.integers(-eps, eps), "clock_offset")
        self.ledger: list[dict] = []
        self.keys: dict[str, dict] = {}
        self.inflight: dict[int, dict] = {}

    # ------------------------------------------------------------------ helpers

    def draw(self, strategy, label: str):
        return self.data.draw(strategy, label=label)

    def at(self, t: int, kind: str, **kw) -> None:
        heapq.heappush(self.q, (t, next(self.seq), kind, kw))

    def snapshot(self) -> Snapshot:
        x = self.x
        return Snapshot(KEY, FP, x["state"], x["attempt_count"], x["verify_attempts"], x["segment_no"],
                        dt(x["anchor"]) if x["anchor"] is not None else None,
                        dt(x["max_deadline"]) if x["max_deadline"] is not None else None,
                        x["absence_proven_through"], x["retry_basis"], x["provider_txn_id"], x["escalation_reason"],
                        tuple(self.attempts))

    def row(self) -> dict[str, Any]:
        s = self.snapshot()
        return {"action_key": KEY, "fingerprint": FP, "state": s.state.value, "attempt_count": s.attempt_count,
                "verify_attempts": s.verify_attempts, "segment_no": s.segment_no, "anchor": s.anchor,
                "max_deadline": s.max_deadline, "absence_proven_through": s.absence_proven_through,
                "retry_basis": s.retry_basis, "provider_txn_id": s.provider_txn_id,
                "escalation_reason": s.escalation_reason}

    def apply(self, d: ctl.Decision) -> None:
        targets = {"T3": State.SUCCEEDED, "T4": State.DECLINED, "T5": State.UNKNOWN, "T6": State.VERIFYING,
                   "T7": State.SUCCEEDED, "T8": State.DECLINED, "T9": State.NOT_EXECUTED, "T10": State.RETRY_WAIT,
                   "T11": State.UNKNOWN, "T12": State.RETRY_WAIT, "T13a": State.UNKNOWN, "T14": State.ESCALATED,
                   "T15": State.SUCCEEDED, "T15a": State.ESCALATED, "TW": State.UNKNOWN, "TA": State.SUCCEEDED}
        self.log.append(f"t={self.t} {self.x['state'].value} --{d.transition}/{d.rule_id}--> {targets[d.transition].value} {d.detail}")
        for k, val in d.patch.items():
            if k in ("next_action_at",):
                self.x[k] = to_ms(val) if val is not None else None
            elif k in self.x:
                self.x[k] = val
        self.x["state"] = targets[d.transition]
        if self.x["state"] not in (State.DISPATCHING, State.VERIFYING):
            self.x["lease"] = None
        if d.anomaly:
            self.anomalies.append(d.anomaly)

    # ------------------------------------------------------------------ controller side (mirrors engine.py)

    def controller_step(self) -> bool:
        x = self.x
        if x["lease"] is not None or x["state"] in TERMINAL or x["state"] == State.ESCALATED:
            return False
        if x["next_action_at"] is not None and x["next_action_at"] > self.t:
            return False
        if x["state"] in (State.UNKNOWN, State.RETRY_WAIT) and self.late:
            self.consume_late()
            return True
        if x["state"] == State.PREPARED:
            self.claim(Basis.INITIAL)
        elif x["state"] == State.RETRY_WAIT:
            self.claim(Basis(x["retry_basis"]))
        elif x["state"] == State.UNKNOWN:
            d = ctl.decide(self.snapshot(), self.c, self.p, dt(self.t))
            if d.transition == "T6":
                self.apply(d)
                self.x["lease"] = ("verify",)
                self.verify()
            else:
                self.apply(d)
        return True

    def claim(self, basis: Basis) -> None:
        p, x = self.p, self.x
        if basis != Basis.INITIAL:
            ok, rule, why = ctl.claim_guard(self.c, p)(self.row(), dt(self.t), dt(self.t + p.attempt_ms))
            if not ok:
                self.apply(ctl.Decision(rule, "T13a", {"next_action_at": None}, detail=why))
                return
        n = x["attempt_count"] + 1
        d = self.t + p.attempt_ms
        new_segment = basis.starts_segment
        x["attempt_count"] = n
        x["segment_no"] += 1 if new_segment else 0
        x["anchor"] = self.t if new_segment else x["anchor"]
        x["max_deadline"] = max(x["max_deadline"] or d, d)
        x["retry_basis"] = basis.value
        x["next_action_at"] = None
        x["state"] = State.DISPATCHING
        x["lease"] = ("dispatch", n)
        self.attempts.append(AttemptView(n, dt(d), x["segment_no"], basis.value))
        self.log.append(f"t={self.t} claim attempt {n} basis={basis.value} d={d}")
        self.at(d + 1, "SWEEP", n=n)
        # Zombie pause between claim and send (F7). In BOUNDED mode with B1 honoured, the whole
        # claim→commit path must fit before d (see B1 in the design).
        bounded_b1 = p.mode == "BOUNDED" and self.v.b1 and not ctl.d_eff(self.c, p)
        pause = self.draw(st.sampled_from([0, 0, 0, 0, 0, 0, "zombie"]), "pause")
        if pause == "zombie":
            hi = p.attempt_ms - 10 if bounded_b1 else p.attempt_ms + 6000
            pause = self.draw(st.integers(0, max(hi, 0)), "zombie_ms")
        send = self.t + pause
        # engine: the HTTP timeout (remaining budget − margin) starts when the request is actually sent,
        # i.e. after any PAUSE inside the client — exactly what lets a zombie send late.
        self.workers[n] = Worker(n, d, send + p.attempt_ms - p.margin_ms)
        if bounded_b1:
            eps = p.clock_skew_ms * self.v.skew_factor
            net = self.draw(st.integers(0, max(d - send - eps - 5, 0)), "net_delay")
        else:
            net = self.draw(st.one_of(st.integers(0, 30), st.integers(0, 8000)), "net_delay")
        self.at(self.workers[n].waiting_until, "WORKER_TIMEOUT", n=n)
        self.at(send + net, "ARRIVE", n=n, d=d)

    def verify(self) -> None:
        err = self.draw(st.sampled_from([False, False, False, True]), "lookup_error")
        lat = self.draw(st.integers(0, 40), "lookup_latency")
        as_of_p = self.t + lat + self.off
        self.t += lat
        if err:
            r = LookupView(False, None, ())
        else:
            visible = [rec for rec in self.ledger if rec["commit_p"] + rec["lag"] <= as_of_p]
            r = LookupView(True, dt(as_of_p), tuple(
                {"client_reference": KEY, "status": rec["status"], "provider_txn_id": rec["txn"]} for rec in visible))
        self.apply(ctl.on_lookup(self.snapshot(), r, self.c, self.p, dt(self.t)))

    def consume_late(self) -> None:
        late, self.late = self.late, []
        for attempt_no, body in late:
            d = ctl.late_success(self.snapshot(), attempt_no, body)
            if d is None:
                self.late.append((attempt_no, body))
                continue
            if d.transition:
                self.apply(d)
            elif d.anomaly:
                self.anomalies.append(d.anomaly)

    def worker_result(self, n: int, obs: Observation, body: dict | None) -> None:
        w = self.workers[n]
        if w.done:
            return
        w.done = True
        if self.x["lease"] == ("dispatch", n):
            txn = body.get("provider_txn_id") if body else None
            self.apply(ctl.on_dispatch_result(self.snapshot(), obs, self.c, self.p, provider_txn_id=txn))
        elif obs == Observation.COMMITTED:  # fenced: keep as late evidence
            self.late.append((n, body))

    # ------------------------------------------------------------------ provider side

    def provider_now(self) -> int:
        return self.t + self.off

    def enforce(self) -> bool:
        return ctl.d_eff(self.c, self.p) and not self.v.ignore_deadline

    def respond(self, n: int, obs: Observation, body: dict | None) -> None:
        if self.draw(st.sampled_from([False, False, True]), "response_lost"):
            return
        self.at(self.t + self.draw(st.integers(0, 200), "response_delay"), "RESPONSE", n=n, obs=obs, body=body)

    def arrive(self, n: int, d: int) -> None:
        now_p = self.provider_now()
        if self.enforce() and now_p > d:
            return self.respond(n, Observation.DEADLINE_REJECTED, None)
        key = KEY if self.c.idempotency else None
        if key is not None:
            rec = self.keys.get(key)
            retention = self.v.retention_ms if self.v.retention_ms is not None else self.c.key_ttl_ms
            if rec and rec["state"] == "COMPLETED" and retention is not None and now_p >= rec["created"] + retention:
                del self.keys[key]
                rec = None
            if rec and rec["state"] == "COMPLETED":
                return self.respond(n, rec["obs"], rec["body"])
            if rec and rec["state"] == "IN_PROGRESS":
                return self.respond(n, Observation.AMBIGUOUS, None)  # 409 IDEMPOTENCY_IN_PROGRESS
            self.keys[key] = {"state": "IN_PROGRESS", "created": now_p, "owner": n}
        outcome = self.draw(st.sampled_from(["commit", "commit", "commit", "decline", "abort"]), "outcome")
        proc = self.draw(st.one_of(st.integers(0, 50), st.integers(0, 3000)), "processing")
        if self.p.mode == "BOUNDED" and self.v.b1 and not self.v.b1_arrival_only and not ctl.d_eff(self.c, self.p):
            proc = min(proc, max(d - now_p - 1, 0))  # B1 (as corrected): the commit itself happens by d
        self.at(self.t + proc, "COMMIT", n=n, d=d, outcome=outcome, key=key)

    def commit(self, n: int, d: int, outcome: str, key: str | None) -> None:
        now_p = self.provider_now()
        if outcome == "abort" or (self.enforce() and now_p > d):
            if key is not None and self.keys.get(key, {}).get("owner") == n and self.keys[key]["state"] == "IN_PROGRESS":
                del self.keys[key]  # K6
            obs = Observation.DEADLINE_REJECTED if outcome != "abort" else Observation.AMBIGUOUS
            return self.respond(n, obs, None)
        status = "succeeded" if outcome == "commit" else "declined"
        txn = f"pay-{n}-{len(self.ledger)}"
        lag = self.draw(st.integers(0, (self.c.lookup_lag_ms or 0) * self.v.lookup_lag_factor), "lookup_lag")
        self.ledger.append({"txn": txn, "status": status, "commit_p": now_p, "lag": lag, "attempt": n})
        body = {"client_reference": KEY, "status": status, "provider_txn_id": txn, **PAYLOAD}
        obs = Observation.COMMITTED if status == "succeeded" else Observation.DECLINED
        if key is not None:
            self.keys[key] = {"state": "COMPLETED", "created": self.keys[key]["created"], "owner": n, "obs": obs, "body": body}
        self.respond(n, obs, body)

    # ------------------------------------------------------------------ run

    def run(self, max_events: int = 400) -> None:
        for _ in range(max_events):
            while self.controller_step():
                pass
            if not self.q:
                nxt = self.x["next_action_at"]
                if nxt is not None and nxt > self.t and self.x["state"] in (State.UNKNOWN, State.RETRY_WAIT):
                    self.t = nxt
                    continue
                return
            t, _, kind, kw = heapq.heappop(self.q)
            nxt = self.x["next_action_at"]
            if nxt is not None and self.t < nxt <= t and self.x["lease"] is None:
                heapq.heappush(self.q, (t, next(self.seq), kind, kw))
                self.t = nxt
                continue
            self.t = max(self.t, t)
            if kind == "ARRIVE":
                self.arrive(kw["n"], kw["d"])
            elif kind == "COMMIT":
                self.commit(kw["n"], kw["d"], kw["outcome"], kw["key"])
            elif kind == "RESPONSE":
                w = self.workers[kw["n"]]
                if not w.done and self.t <= w.waiting_until:
                    self.worker_result(kw["n"], kw["obs"], kw["body"])
            elif kind == "WORKER_TIMEOUT":
                self.worker_result(kw["n"], Observation.AMBIGUOUS, None)
            elif kind == "SWEEP":
                if self.x["lease"] == ("dispatch", kw["n"]):
                    self.apply(ctl.Decision("SWEEP_LEASE_EXPIRED", "T5", {"ambiguity_cause": "LEASE_EXPIRED"}))
        raise AssertionError("simulation did not quiesce within the event budget:\n" + "\n".join(self.log[-30:]))

    # ------------------------------------------------------------------ verdicts

    @property
    def effects(self) -> int:
        return sum(1 for r in self.ledger if r["status"] == "succeeded")

    def explain(self) -> str:
        return "\n".join(self.log) + f"\nledger={self.ledger}\nanomalies={self.anomalies}"


def caps() -> st.SearchStrategy[Capability]:
    return st.builds(
        lambda i, tau, q, d: Capability(idempotency=i, key_ttl_ms=tau if i else None, lookup=q,
                                        lookup_lag_ms=500 if q != LookupMode.NONE else None, deadline_enforced=d),
        st.booleans(), st.sampled_from([None, 3000, 6000]), st.sampled_from(list(LookupMode)), st.booleans(),
    )
