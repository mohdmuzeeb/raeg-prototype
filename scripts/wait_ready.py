"""Host-side readiness wait used after `docker compose up --wait` (N56).

`--wait` checks health inside the containers; the host port forward (colima) can come up slightly later,
so tests that start immediately could see "connection refused". Polls each URL until HTTP 200.

usage: uv run python scripts/wait_ready.py <url> [<url> ...]
"""

from __future__ import annotations

import sys
import time
import urllib.request

TIMEOUT_S = 240


def main() -> int:
    deadline = time.monotonic() + TIMEOUT_S
    for url in sys.argv[1:]:
        while True:
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    if r.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() > deadline:
                print(f"not ready after {TIMEOUT_S}s: {url}", file=sys.stderr)
                return 1
            time.sleep(0.1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
