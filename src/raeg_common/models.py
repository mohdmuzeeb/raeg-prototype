"""Shared domain models (DESIGN §2). Pure data; no I/O."""

from __future__ import annotations

import enum
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from raeg_common.fingerprint import fingerprint

ActionKey = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:\-]+$")]


class PaymentPayload(BaseModel):
    """p_i: immutable payment parameters. Amounts are integer minor units; never floats."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount_minor: int = Field(gt=0, le=2**53 - 1)
    currency: str
    payer_id: str = Field(min_length=1, max_length=64)
    payee_id: str = Field(min_length=1, max_length=64)
    memo: str = Field(default="", max_length=256)

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Z]{3}", v):
            raise ValueError("currency must be an ISO-4217 style 3-letter uppercase code")
        return v

    @field_validator("amount_minor", mode="before")
    @classmethod
    def _no_float_amounts(cls, v: object) -> object:
        if isinstance(v, float) or isinstance(v, bool):
            raise ValueError("amount_minor must be an integer")
        return v

    def fingerprint(self) -> str:
        return fingerprint(self.model_dump())


class Action(BaseModel):
    """a_i = (k_i, p_i, s_i); s_i lives in the gateway store, not here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_key: ActionKey
    payload: PaymentPayload

    @property
    def fingerprint(self) -> str:
        return self.payload.fingerprint()


class LookupMode(str, enum.Enum):
    NONE = "none"
    BY_PROVIDER_ID = "by_provider_id"
    BY_CLIENT_REF = "by_client_ref"


class Capability(BaseModel):
    """c = (I, τ, Q, δ, D) as *declared* by a provider profile (DESIGN §2.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency: bool  # I
    key_ttl_ms: int | None = Field(default=None, gt=0)  # τ; None = ∞ (≫ run length)
    lookup: LookupMode  # Q
    lookup_lag_ms: int | None = Field(default=None, ge=0)  # δ; required iff lookup is usable
    deadline_enforced: bool  # D

    @model_validator(mode="after")
    def _consistency(self) -> Capability:
        if not self.idempotency and self.key_ttl_ms is not None:
            raise ValueError("key_ttl_ms is meaningless without idempotency")
        if self.lookup != LookupMode.NONE and self.lookup_lag_ms is None:
            raise ValueError("lookup_lag_ms (δ) must be declared when lookup is available")
        return self

    @property
    def lookup_capable(self) -> bool:
        """Only lookup by client reference can resolve a lost response (§2.3)."""
        return self.lookup == LookupMode.BY_CLIENT_REF


class LatencyModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dist: Literal["constant", "lognormal"] = "lognormal"
    median_ms: float = Field(default=20.0, ge=0)
    sigma: float = Field(default=0.25, ge=0)


class ProviderBehavior(BaseModel):
    """How the synthetic provider behaves when it honours its declared capability."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlap: Literal["reject", "serialize"] = "reject"
    serialize_max_wait_ms: int = Field(default=10_000, gt=0)
    latency: LatencyModel = LatencyModel()
    decline_rate: float = Field(default=0.0, ge=0, le=1)
    actual_lookup_lag_ms: int = Field(default=0, ge=0)
    seed: int = 0


class AssumptionViolations(BaseModel):
    """V1–V4 switches (DESIGN §6.3). Defaults honour the declared capability."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lookup_lag_exceeds_declared_ms: int | None = Field(default=None, ge=0)  # V1: actual lag
    ignore_deadline: bool = False  # V2: D declared but not enforced
    actual_retention_ms: int | None = Field(default=None, gt=0)  # V3: retention < τ
    clock_offset_ms: int = 0  # V4: provider clock offset

    @property
    def any_active(self) -> bool:
        return (
            self.lookup_lag_exceeds_declared_ms is not None
            or self.ignore_deadline
            or self.actual_retention_ms is not None
            or self.clock_offset_ms != 0
        )


class ProviderProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    description: str = ""
    capability: Capability
    behavior: ProviderBehavior = ProviderBehavior()
    violations: AssumptionViolations = AssumptionViolations()

    @model_validator(mode="after")
    def _honest_lag(self) -> ProviderProfile:
        c = self.capability
        if c.lookup != LookupMode.NONE and self.violations.lookup_lag_exceeds_declared_ms is None:
            assert c.lookup_lag_ms is not None
            if self.behavior.actual_lookup_lag_ms >= c.lookup_lag_ms:
                # The commit becomes visible slightly after the trigger-checked commit point,
                # so the actual lag must leave headroom below δ (DESIGN §2.5, L2).
                raise ValueError("behavior.actual_lookup_lag_ms must be < capability.lookup_lag_ms")
        return self


class Observation(str, enum.Enum):
    """Per-attempt observation classes (DESIGN §2.9)."""

    COMMITTED = "COMMITTED"
    DECLINED = "DECLINED"
    DEADLINE_REJECTED = "DEADLINE_REJECTED"
    AMBIGUOUS = "AMBIGUOUS"
    INTERNAL = "INTERNAL"  # e.g. K4 idempotency mismatch — escalate, never retry


class Claim(str, enum.Enum):
    """Normalised claim taxonomy (DESIGN §6.1)."""

    SUCCESS = "SUCCESS"
    NO_EFFECT_DECLINED = "NO_EFFECT(declined)"
    NO_EFFECT_VERIFIED = "NO_EFFECT(verified)"
    UNRESOLVED_ESCALATED = "UNRESOLVED(escalated)"
    UNRESOLVED_GAVE_UP = "UNRESOLVED(gave_up_ambiguous)"
    UNRESOLVED_HORIZON = "UNRESOLVED(horizon)"
