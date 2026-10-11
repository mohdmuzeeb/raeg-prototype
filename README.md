# RAEG: Recovery-Aware Execution Gateway (research prototype)

This is a prototype for an IEEE paper on the **capability-aware recovery of ambiguous financial transactions initiated by autonomous AI agents**. RAEG decides whether to **verify**, **retry**, or **escalate** after an ambiguous payment outcome. Each decision depends on what the downstream provider guarantees, modelled as the capability profile `(I, τ, Q, δ, D)`: idempotency, key retention, lookup, lookup lag, and deadline enforcement.

> **Status: work in progress.** Milestones M0–M3 are complete (scaffolding, shared models, synthetic payment provider, gateway store). The recovery controller (M4), baselines, and experiments come next. **No experimental results have been produced yet.**

## Documents
- [`docs/DESIGN.md`](docs/DESIGN.md): the formal model, state machine, safety tiers, and evaluation protocol (v0.3; earlier versions are in `docs/archive/`)
- [`docs/IMPLEMENTATION_NOTES.md`](docs/IMPLEMENTATION_NOTES.md): deviations from the design, recorded as they arise
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md): setup, tests, and ports
- [`results/README.md`](results/README.md): what evidence is kept, and where

## Quick start
```sh
uv sync
make up-test     # Postgres ×2 + synthetic provider (Docker; colima context)
make test        # unit + provider conformance + gateway store; evidence saved under results/tests/
```

The synthetic provider's guarantees are **contracts of the test environment**. They are not claims about any real payment API (see DESIGN §2.7a).
