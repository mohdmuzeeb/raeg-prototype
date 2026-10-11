"""B3 / B3L workflows (DESIGN §5.4).

plain           activity RetryPolicy(max_dispatch); declines non-retryable.
lookup_delta    on ambiguity: sleep δ (documented lag), look up by client_reference; found → done,
                else re-dispatch. No deadline or TTL reasoning, no DD rule, no escalation.
lookup_horizon  same, but sleeps until max(d_j) + δ over the deadlines it has sent (stronger baseline).
The workflow returns a native-outcome dict in every case (it never fails), so results are uniform.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from systems.temporal_wf.activities import DispatchInput, LookupInput, dispatch_payment, lookup_payment


def _cause_type(err: ActivityError) -> str:
    cause = err.cause
    return cause.type if isinstance(cause, ApplicationError) and cause.type else type(cause).__name__


@workflow.defn
class PaymentWorkflow:
    @workflow.run
    async def run(self, inp: dict) -> dict:
        variant = inp["variant"]
        ms = lambda n: timedelta(milliseconds=n)  # noqa: E731
        base = dict(system_id=inp["system_id"], action_key=inp["action_key"], payload=inp["payload"],
                    attempt_ms=inp["attempt_ms"], margin_ms=inp["margin_ms"], idempotency_key=inp["action_key"])
        if variant == "plain":
            try:
                res = await workflow.execute_activity(
                    dispatch_payment, DispatchInput(**base), start_to_close_timeout=ms(inp["attempt_ms"]),
                    retry_policy=RetryPolicy(initial_interval=ms(inp["backoff_base_ms"]), backoff_coefficient=2.0,
                                             maximum_interval=ms(inp["max_backoff_ms"]),
                                             maximum_attempts=inp["max_dispatch"],
                                             non_retryable_error_types=["Declined", "Internal"]))
                return {"status": "ok", "provider_txn_id": res["provider_txn_id"], "detail": res}
            except ActivityError as err:
                cause = _cause_type(err)
                if cause == "Declined":
                    return {"status": "declined", "detail": {"cause": cause}}
                return {"status": "error", "error_class": cause, "detail": {"cause": cause, "retries_exhausted": True}}

        # lookup-before-retry variants
        delta = inp.get("lookup_lag_ms") or 0
        max_d = None
        for n in range(1, inp["max_dispatch"] + 1):
            deadline = workflow.now() + ms(inp["attempt_ms"])
            max_d = deadline if max_d is None else max(max_d, deadline)
            try:
                res = await workflow.execute_activity(
                    dispatch_payment, DispatchInput(**base, deadline_iso=deadline.isoformat(), attempt_no=n),
                    start_to_close_timeout=ms(inp["attempt_ms"]), retry_policy=RetryPolicy(maximum_attempts=1))
                return {"status": "ok", "provider_txn_id": res["provider_txn_id"], "detail": {**res, "found_by": "response"}}
            except ActivityError as err:
                cause = _cause_type(err)
                if cause == "Declined":
                    return {"status": "declined", "detail": {"cause": cause, "attempt_no": n}}
                if cause == "Internal":
                    return {"status": "error", "error_class": cause, "detail": {"attempt_no": n}}
            wake = workflow.now() + ms(delta) if variant == "lookup_delta" else max_d + ms(delta)
            pause = wake - workflow.now()
            if pause > timedelta(0):
                await workflow.sleep(pause)
            try:
                found = await workflow.execute_activity(
                    lookup_payment, LookupInput(inp["system_id"], inp["action_key"], 1000),
                    start_to_close_timeout=ms(2000),
                    retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=ms(inp["backoff_base_ms"])))
            except ActivityError:
                found = None  # conventional: an unanswered lookup is treated as "not found"
            if found:
                mine = [r for r in found["records"] if r.get("client_reference") == inp["action_key"]]
                ok = [r for r in mine if r.get("status") == "succeeded"]
                if ok:
                    return {"status": "ok", "provider_txn_id": ok[0]["provider_txn_id"],
                            "detail": {"found_by": "lookup", "after_attempt": n, "n_succeeded": len(ok)}}
                if any(r.get("status") == "declined" for r in mine):
                    return {"status": "declined", "detail": {"found_by": "lookup", "after_attempt": n}}
        return {"status": "not_found_after_retries", "detail": {"attempts": inp["max_dispatch"]}}
