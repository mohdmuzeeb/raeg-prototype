from __future__ import annotations

from payment_provider.faults import FaultEngine, FaultKind, FaultPlan


def test_ingress_indices_are_per_reference_and_kind() -> None:
    engine = FaultEngine()
    engine.load(
        FaultPlan.model_validate(
            {
                "plan_id": "t",
                "rules": [
                    {"client_reference": "a", "ingress_index": 2, "fault": "LOST_RESPONSE"},
                    {"client_reference": "a", "ingress_index": 2, "fault": "INGRESS_HOLD", "params": {"hold_ms": 5}},
                    {"client_reference": "a", "kind": "lookup_ref", "ingress_index": 1, "fault": "LOOKUP_UNAVAILABLE"},
                ],
            }
        )
    )
    assert engine.next_ingress("a", "dispatch") == (1, {})
    idx, faults = engine.next_ingress("a", "dispatch")
    assert idx == 2 and set(faults) == {FaultKind.LOST_RESPONSE, FaultKind.INGRESS_HOLD}
    assert engine.next_ingress("b", "dispatch") == (1, {})
    idx, faults = engine.next_ingress("a", "lookup_ref")
    assert idx == 1 and set(faults) == {FaultKind.LOOKUP_UNAVAILABLE}
    assert engine.next_ingress(None, "dispatch") == (None, {})
    engine.reset_counters()
    assert engine.next_ingress("a", "dispatch")[0] == 1
