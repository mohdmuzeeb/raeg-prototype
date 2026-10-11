# RAEG prototype — runbook

## Prerequisites
- macOS with [colima](https://github.com/abiosoft/colima) (Docker runtime), Docker CLI with Compose v2, and [uv](https://docs.astral.sh/uv/).
- Every Make target sets `DOCKER_CONTEXT=colima`, so your global Docker context is left unchanged.
- Start the VM once per boot: `colima start default`. The reference machine is an Apple M1 with 8 GB RAM, running a VM with 4 vCPU and 4 GiB.

## Setup
```sh
uv sync            # creates .venv from uv.lock (Python 3.12)
make up-test       # builds raeg/python:dev; starts provider-db, raeg-db, provider (reset endpoint ON)
```
`make up` starts the same stack with the reset endpoint **off**. Experiment runs must use `make up` and a fresh volume (`make clean`).

## Tests and evidence
| Command | What it runs | Evidence directory |
|---|---|---|
| `make test-unit` | `tests/unit` (no Docker needed) | `results/tests/<UTC>_unit/` |
| `make test-conformance` | `tests/conformance` against the running provider | `results/tests/<UTC>_conformance/` (+ `compose.log`) |
| `make test-gateway` | `tests/gateway`: M3 store transitions, concurrency, auditability, projection rebuild (needs raeg-db) | `results/tests/<UTC>_gateway/` |
| `make test-property` | `tests/property`: model-based safety checks of the controller (Hypothesis; no Docker) | `results/tests/<UTC>_property/` |
| `make test-integration` | `tests/integration`: engine + store + provider scenarios, real-process crashes (CP1–CP3), zombies (F7), concurrent engines | `results/tests/<UTC>_integration/` (+ `compose.log`) |

`tests/conformance/test_provider_restart.py` SIGKILLs and restarts the provider container (K7). It is part of `make test-conformance`.

Each evidence directory contains:
- `manifest.json`: the source-tree SHA-256, git HEAD (if any), lockfile hashes, host, Docker version, image IDs, and the pass/fail status;
- `pytest.log`: the full output;
- `junit.xml`.

Failed runs are kept. Nothing under `results/` is edited or deleted by tooling. From M4 on, the runner writes the absolute repository path as `<repo>` and the home directory as `~` in the logs it captures, and lists these substitutions under `redactions` in `manifest.json`. Logs captured earlier are unredacted.

## Ports (bound to 127.0.0.1 only)
| Port | Service |
|---|---|
| 18001 | provider public API (`/payments`) |
| 19001 | provider oracle/admin (`/oracle/*`, `/admin/*`; header `x-oracle-token`) |
| 55433 | provider Postgres |
| 55432 | RAEG Postgres (gateway store; the app connects as `raeg_app`) |

## Clock note
The colima VM clock is not synchronised with the macOS host clock; an offset of roughly 1.5 s was observed on 2026-10-11. Every service runs in the same VM and therefore shares one kernel clock. Host-side tests and harness code must take times from `/oracle/now` (the provider clock), never from the host.
