"""Native outcome -> normalized claim (DESIGN §6.1). Versioned; the native outcome is always kept
next to the claim so that reviewers can re-score."""

from __future__ import annotations

from raeg_common.models import Claim

MAPPING_VERSION = "claims-v1"

_BY_STATUS = {
    "ok": Claim.SUCCESS,
    "declined": Claim.NO_EFFECT_DECLINED,
    "not_executed": Claim.NO_EFFECT_VERIFIED,             # RAEG NOT_EXECUTED (authoritative absence)
    "not_found_after_retries": Claim.NO_EFFECT_VERIFIED,  # B3L: believes a lookup miss
    "escalated": Claim.UNRESOLVED_ESCALATED,
    "error": Claim.UNRESOLVED_GAVE_UP,                    # B1/B2/B3 retries exhausted on ambiguity
    "pending": Claim.UNRESOLVED_HORIZON,
}


def normalize(native: dict) -> Claim:
    status = native.get("status")
    if status == "error" and native.get("error_class") == "INTERNAL":
        return Claim.UNRESOLVED_GAVE_UP
    try:
        return _BY_STATUS[status]
    except KeyError as exc:
        raise ValueError(f"unmapped native status {status!r} ({MAPPING_VERSION})") from exc


def pessimistic(claim: Claim) -> Claim:
    """Secondary 'pessimistic-agent rescoring' (§6.2): gave-up errors read as failures. Never merged
    with primary metrics."""
    return Claim.NO_EFFECT_ASSUMED if claim == Claim.UNRESOLVED_GAVE_UP else claim
