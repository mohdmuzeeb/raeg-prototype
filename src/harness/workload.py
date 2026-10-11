"""Seeded workload generation (DESIGN §5.5): open-loop Poisson arrivals of business intents.

The workload depends only on (seed, workload config): every system in a seed sees exactly the same
intents, keys, payloads, arrival offsets and duplicate-submission groups.
"""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class WorkloadConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    n_actions: int = Field(gt=0)
    arrival_rate_per_s: float = Field(gt=0)
    duplicate_fraction: float = Field(default=0.0, ge=0, le=1)  # ρ: intents submitted c times concurrently
    duplicate_copies: int = Field(default=2, ge=2)  # c
    amount_min_minor: int = Field(default=100, gt=0)
    amount_max_minor: int = Field(default=10_000, gt=0)
    n_payers: int = Field(default=50, gt=0)


@dataclass(frozen=True)
class Intent:
    index: int
    action_key: str
    arrival_offset_ms: int
    copies: int  # 1 = single submission; c > 1 = concurrent duplicate submissions (F5)
    payload: dict

    def to_json(self) -> dict:
        return asdict(self)


def generate(seed: int, cfg: WorkloadConfig, namespace: str) -> list[Intent]:
    """`namespace` (e.g. the cell id) makes keys unique across cells while staying system-independent."""
    rng = random.Random(f"workload|{seed}|{namespace}")
    t = 0.0
    out = []
    for i in range(cfg.n_actions):
        t += rng.expovariate(cfg.arrival_rate_per_s)
        key = str(uuid.UUID(int=rng.getrandbits(128), version=5))  # deterministic, UUID-shaped intent key
        copies = cfg.duplicate_copies if rng.random() < cfg.duplicate_fraction else 1
        payload = {
            "amount_minor": rng.randint(cfg.amount_min_minor, cfg.amount_max_minor),
            "currency": "USD",
            "payer_id": f"payer-{rng.randrange(cfg.n_payers):03d}",
            "payee_id": "merchant-001",
            "memo": f"intent-{i:05d}",
        }
        out.append(Intent(i, f"{namespace}-{key}", int(t * 1000), copies, payload))
    return out


def write(path: Path, intents: list[Intent]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for intent in intents:
            fh.write(json.dumps(intent.to_json(), sort_keys=True) + "\n")


def read(path: Path) -> list[Intent]:
    return [Intent(**json.loads(line)) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
