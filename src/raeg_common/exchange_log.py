"""Append-only JSONL exchange log (DESIGN §7, `client_exchanges.jsonl`).

Each record is written with a single os.write() on an O_APPEND descriptor, so it reaches the
kernel before the call returns and survives a subsequent os._exit() at a crash hook.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class ExchangeLog:
    def __init__(self, path: str | None) -> None:
        self._fd: int | None = None
        self._lock = threading.Lock()
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)

    def write(self, record: dict[str, Any]) -> None:
        if self._fd is None:
            return
        record = {"logged_at": utc_now_iso(), **record}
        line = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with self._lock:
            os.write(self._fd, line)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
