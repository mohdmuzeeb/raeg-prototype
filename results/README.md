# results/

| Directory | In git? | Content |
|---|---|---|
| `tests/<UTC>_<suite>/` | yes | Test evidence: `manifest.json` (provenance and status), `pytest.log`, `junit.xml`, `compose.log`. Failed and aborted runs are kept; aborted runs carry an `ABORTED.txt`. |
| `raw/<run_id>/` | **no** (too large) | Immutable raw experiment outputs (DESIGN §7). Each run is archived outside git as `<run_id>.tar.zst`, alongside a `SHA256SUMS` file. The archive location and checksums are recorded in `docs/EXPERIMENT_LOG.md` (from M6 on). |
| `derived/` | **no** | Regenerated deterministically from `raw/` by `analysis/` (`make analyze`). |

Nothing under `results/` is edited by hand.
