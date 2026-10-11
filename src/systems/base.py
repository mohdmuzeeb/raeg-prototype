"""Shared configuration, native outcome format, and the system interface (DESIGN §5.4, §6.1)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

import yaml

from raeg_common.exchange_log import ExchangeLog
from raeg_common.hooks import HookPlan, ReloadingHookPlan
from raeg_common.models import PaymentPayload
from raeg_common.provider_client import ProviderClient

ROOT = Path(__file__).resolve().parents[2]
SYSTEMS_FILE = ROOT / "config" / "systems.yaml"

# Native terminal statuses. Mapping to normalized claims: analysis/claims.py.
OK, DECLINED, ERROR, NOT_FOUND_AFTER_RETRIES, ESCALATED, NOT_EXECUTED, PENDING = (
    "ok", "declined", "error", "not_found_after_retries", "escalated", "not_executed", "pending")


@dataclass(frozen=True)
class SystemConfig:
    system_id: str
    kind: str
    attempt_ms: int
    margin_ms: int
    client_timeout_ms: int
    max_dispatch: int
    backoff_base_ms: int
    max_backoff_ms: int
    send_deadline: bool
    idempotency_key: bool = False
    variant: str | None = None
    policy: dict[str, Any] = field(default_factory=dict)
    description: str = ""

    def backoff_ms(self, n: int) -> int:
        return min(self.backoff_base_ms * 2 ** max(n - 1, 0), self.max_backoff_ms)


def load_systems(path: Path = SYSTEMS_FILE) -> dict[str, SystemConfig]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    common = raw["common"]
    out = {}
    for sid, spec in raw["systems"].items():
        merged = {**common, **spec}
        out[sid] = SystemConfig(system_id=sid, **merged)
    return out


@dataclass
class Native:
    """A system's native terminal (or pending) output, kept verbatim next to the normalized claim."""

    system: str
    action_key: str
    status: str
    provider_txn_id: str | None = None
    error_class: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"system": self.system, "action_key": self.action_key, "status": self.status,
                "provider_txn_id": self.provider_txn_id, "error_class": self.error_class, "detail": self.detail}


class SystemUnderTest(Protocol):
    config: SystemConfig

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def submit(self, action_key: str, payload: PaymentPayload, profile_id: str) -> Native: ...
    async def status(self, action_key: str) -> Native: ...


def make_client(config: SystemConfig, *, provider_url: str | None = None, worker_tag: str = "") -> ProviderClient:
    """The shared provider client, with hooks and exchange log configured from the environment."""
    hooks_path = os.environ.get("HOOK_PLAN")
    log_dir = os.environ.get("EXCHANGE_LOG_DIR")
    return ProviderClient(
        provider_url or os.environ.get("PROVIDER_URL", "http://provider:8001"),
        config.system_id,
        timeout_s=config.client_timeout_ms / 1000,
        hooks=ReloadingHookPlan(hooks_path) if hooks_path else HookPlan(),
        log=ExchangeLog(os.path.join(log_dir, f"client_exchanges-{config.system_id}{worker_tag}.jsonl") if log_dir else None),
        send_deadline=config.send_deadline,
    )


def with_overrides(config: SystemConfig, **kw: Any) -> SystemConfig:
    return replace(config, **kw)
