from __future__ import annotations

import pytest
from pydantic import ValidationError

from raeg_common.models import Capability, LookupMode, PaymentPayload, ProviderProfile
from raeg_common.profiles import load_all

EXPECTED = {
    # id: (I, τ, Q, δ declared?, D)  — DESIGN §5.3 profile table
    "P1": (True, None, LookupMode.BY_CLIENT_REF, True, True),
    "P2": (True, 3000, LookupMode.BY_CLIENT_REF, True, True),
    "P3": (True, None, LookupMode.NONE, False, False),
    "P4": (False, None, LookupMode.BY_CLIENT_REF, True, True),
    "P4n": (False, None, LookupMode.BY_CLIENT_REF, True, False),
    "P5": (False, None, LookupMode.BY_PROVIDER_ID, True, False),
    "P6": (False, None, LookupMode.NONE, False, False),
    "P7": (True, 3000, LookupMode.NONE, False, True),
}


def test_profiles_match_design_table() -> None:
    profiles = load_all()
    assert set(profiles) == set(EXPECTED)
    for pid, (i, tau, q, has_delta, d) in EXPECTED.items():
        c = profiles[pid].capability
        assert (c.idempotency, c.key_ttl_ms, c.lookup, c.lookup_lag_ms is not None, c.deadline_enforced) == (i, tau, q, has_delta, d), pid
        assert not profiles[pid].violations.any_active, f"{pid}: shipped profiles must honour their declarations"


def test_only_client_ref_lookup_is_recovery_capable() -> None:
    profiles = load_all()
    assert {p for p, prof in profiles.items() if prof.capability.lookup_capable} == {"P1", "P2", "P4", "P4n"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"amount_minor": 10.0, "currency": "USD", "payer_id": "a", "payee_id": "b"},
        {"amount_minor": 0, "currency": "USD", "payer_id": "a", "payee_id": "b"},
        {"amount_minor": True, "currency": "USD", "payer_id": "a", "payee_id": "b"},
        {"amount_minor": 1, "currency": "usd", "payer_id": "a", "payee_id": "b"},
        {"amount_minor": 1, "currency": "USD", "payer_id": "a", "payee_id": "b", "extra": 1},
    ],
)
def test_payload_rejects_invalid(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        PaymentPayload(**kwargs)


def test_capability_consistency_rules() -> None:
    with pytest.raises(ValidationError):
        Capability(idempotency=False, key_ttl_ms=100, lookup=LookupMode.NONE, deadline_enforced=False)
    with pytest.raises(ValidationError):
        Capability(idempotency=True, lookup=LookupMode.BY_CLIENT_REF, deadline_enforced=True)  # δ missing


def test_actual_lag_must_leave_headroom_below_declared_delta() -> None:
    base = {
        "id": "X",
        "capability": {"idempotency": True, "lookup": "by_client_ref", "lookup_lag_ms": 500, "deadline_enforced": True},
    }
    with pytest.raises(ValidationError):
        ProviderProfile.model_validate({**base, "behavior": {"actual_lookup_lag_ms": 500}})
    # ...unless V1 deliberately violates the declaration
    ProviderProfile.model_validate(
        {**base, "behavior": {"actual_lookup_lag_ms": 500}, "violations": {"lookup_lag_exceeds_declared_ms": 900}}
    )
