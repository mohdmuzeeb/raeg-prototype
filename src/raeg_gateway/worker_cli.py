"""Run the recovery engine for one action in its own OS process (used by real-crash tests).

The crash hooks of the shared client call os._exit(137), so a hook kills this process outright:
no cleanup, no further database writes, exactly like a fail-stop crash.

usage: python -m raeg_gateway.worker_cli --app-dsn DSN --provider-url URL --worker-id W --action K [--hooks plan.json]
"""

from __future__ import annotations

import argparse
import asyncio

from raeg_common.exchange_log import ExchangeLog
from raeg_common.hooks import HookPlan
from raeg_common.provider_client import ProviderClient
from raeg_gateway.engine import RecoveryEngine
from raeg_gateway.store import GatewayStore, create_pool


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-dsn", required=True)
    ap.add_argument("--provider-url", required=True)
    ap.add_argument("--worker-id", required=True)
    ap.add_argument("--action", required=True)
    ap.add_argument("--hooks")
    ap.add_argument("--exchange-log")
    ap.add_argument("--code-sha", default="unknown")
    ap.add_argument("--timeout-s", type=float, default=30.0)
    args = ap.parse_args()
    pool = await create_pool(args.app_dsn, max_size=4)
    hooks = HookPlan.from_file(args.hooks) if args.hooks else HookPlan()
    client = ProviderClient(args.provider_url, args.worker_id, timeout_s=args.timeout_s, hooks=hooks,
                            log=ExchangeLog(args.exchange_log))
    engine = RecoveryEngine(GatewayStore(pool, args.code_sha, "cli"), client, args.worker_id)
    state = await engine.run_to_quiescence(args.action)
    print(f"final_state={state.value}")
    await client.aclose()
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
