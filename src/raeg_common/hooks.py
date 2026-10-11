"""Crash and pause hooks in the shared provider client (DESIGN §4, faults F2/F6/F7).

Every system under test calls the provider through the same client, so a fault plan entry
(action_key, attempt_no) -> hook fires at the *same* point for RAEG and for every baseline.

  CP1   after the caller's write-ahead (if any), before the request is sent
  PAUSE between claim and send (zombie, F7): sleeps, then sends anyway
  CP2   after the request has been handed to the transport, before the response arrives
  CP3   after the response is received, before it is returned to the caller (i.e. before persist)
"""

from __future__ import annotations

import asyncio
import enum
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class HookPoint(str, enum.Enum):
    CP1 = "CP1"
    CP2 = "CP2"
    CP3 = "CP3"
    PAUSE = "PAUSE"


@dataclass(frozen=True)
class HookAction:
    point: HookPoint
    pause_ms: int = 0  # PAUSE only
    after_send_ms: int = 0  # CP2 only: how long after handing the request to the transport

    def to_json(self) -> dict:
        return {"point": self.point.value, "pause_ms": self.pause_ms, "after_send_ms": self.after_send_ms}


def _hard_exit() -> None:
    os._exit(137)  # fail-stop: no cleanup, no flush of Python buffers


class HookPlan:
    def __init__(
        self,
        entries: dict[tuple[str, int], HookAction] | None = None,
        exit_fn: Callable[[], None] = _hard_exit,
    ) -> None:
        self._entries = dict(entries or {})
        self._exit_fn = exit_fn

    @classmethod
    def from_file(cls, path: str | Path, exit_fn: Callable[[], None] = _hard_exit) -> HookPlan:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        entries = {}
        for item in raw.get("hooks", []):
            action = HookAction(
                point=HookPoint(item["point"]),
                pause_ms=int(item.get("pause_ms", 0)),
                after_send_ms=int(item.get("after_send_ms", 0)),
            )
            entries[(item["action_key"], int(item["attempt_no"]))] = action
        return cls(entries, exit_fn)

    def lookup(self, action_key: str, attempt_no: int) -> HookAction | None:
        return self._entries.get((action_key, attempt_no))

    def crash(self) -> None:
        self._exit_fn()

    @staticmethod
    async def pause(action: HookAction) -> None:
        await asyncio.sleep(action.pause_ms / 1000)
