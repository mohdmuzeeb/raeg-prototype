# Experimental protocol changes

Every change to the evaluation protocol after approval is recorded here: what changed, why, the evidence
behind it, who approved it, and how to reproduce runs made under the earlier protocol. Original
configurations are never deleted.

## PC-1 … PC-4 (M7, approved 2026-10-11)

**Evidence.** The timed calibration batch `pilot-calibration` (12 runs, seed 101, commit `ccc9314`;
archive `results/archive/pilot-calibration-20261011.tar.gz`, sha256 `4393be99…fe50`). With FM at φ = 0.2,
the process-crash faults F2/F6 caused about a dozen process crashes per 200-action run. For B1 and RAEG,
whose API ran in the crashing process, this produced 16–23 s outage windows and 68–101 of 200 intents
abandoned by the agent (5 resubmits, 1 s apart). B3/B3L only lost their Temporal worker (0 transport errors).
FM cells would therefore have measured restart outages and the agent's give-up rule, with exposure very
uneven across architectures. Every intent RAEG did receive ended terminal, with no Tier S or claim
violations (verified from `raeg/executions.csv`; audit misclassification fixed as IMPLEMENTATION_NOTES N50).

| # | Change | Before | After |
|---|---|---|---|
| PC-1 | FM mixture versioned | faults-v1: FM = {F1, F2, F3, F4, F6, F9} + F5 | faults-v2: FM = {F1, F3, F4, F9} + F5. v1 stays selectable as `FMv1`; `fault_plan.json` records `fault_config_version`. |
| PC-2 | Agent retry model | ≤ 5 resubmits, 1 s apart (≈ 5 s) | Exponential backoff 1 s → 8 s cap until the **agent horizon** = 60 s per intent from its first submission. Identical for every system; `max_resubmits` = 50 is only a safety cap. |
| PC-3 | Dedicated crash experiment FC | crashes were part of FM | **FC-worker**: 3 planned crashes per run of the process *executing* the attempt (hooks CP1/CP2/CP3 on the intents arriving nearest 5 s, 27 s, 49 s; ≥ 20 s apart). **FC-api**: 3 harness-triggered crashes of the driver-facing API process at the same offsets. FC runs use 3 arrivals/s so that arrivals span ≥ 49 s. |
| PC-4 | RAEG topology | API and recovery engine in one process | API process (records T1, waits) + separate `raeg-worker` process (DESIGN §5.1), so RAEG and Temporal both have worker-only crashes. B1/B2 have no separate worker: their FC-worker crash *is* an API crash (reported as such). |

**Reporting rules added.**
- Outcomes are reported over all generated intents and, separately, over intents the system accepted (`accepted` = it answered at least one submission or poll).
- The primary recovery comparison under crashes uses FC-worker, where an equivalent execution-worker failure exists (RAEG, B3, B3L). FC-api results are reported separately as topology-dependent API availability.

**Configurations.**
- `config/experiments/pilot-v1.yaml` is the original pilot, never run, with FM written as `FMv1`.
- `config/experiments/pilot.yaml` is the revised pilot.
- `config/experiments/pilot-calibration.yaml` ran at `ccc9314` under faults-v1 and the v1 agent model. Check out that commit to reproduce it exactly.
- `config/experiments/pilot-recalibration.yaml` re-measures under the revised protocol.

## PC-5 (end of experimentation, owner decision 2026-10-11)

**Decision.** RAEG experimentation stops at M7. M8 is not run: no confirmatory evaluation, no further benchmarks,
and no new latency experiments. The ICICyTA 2026 paper uses the existing 280-run M7 pilot dataset.

**What changes relative to DESIGN §8.3.**

| Before | After |
|---|---|
| Pilot data are excluded from paper tables and serve only to choose N and R for M8 | The M7 pilot is the paper's dataset, labelled **exploratory** everywhere it is reported |
| Precision target (R ≥ 5, ≥ 100 faulted actions per seed, CI half-width ≤ 3 pp) to be met by M8 | **Not relaxed.** Every estimate is checked against it; all 56 recovery-completion estimates fail it (R = 3; 33–49 faulted actions per seed), and this is reported |
| Hypotheses frozen in `HYPOTHESES.md` before M8 under the `eval-freeze` tag | No confirmatory hypotheses are tested; there is no `eval-freeze` and no `HYPOTHESES.md`. The results are descriptive |
| Latency compared across all systems | Latency of B3, B3L-δ and B3L-horizon is **invalid** (Temporal cold start, N58) and is not reported |

**Integrity.**
- The run manifests still say `excluded_from_paper: true`, as recorded at run time; they are not edited.
- The analysis reads only the frozen archive `pilot-20261011.tar.gz` (SHA-256 `66da0511…c23a`), after verifying every file against its `SHA256SUMS` (`scripts/analyze_paper.py`).
- The stopped M8 calibration directories are archived separately (`m8-calibration-stopped-20261011.tar.gz`) and are not used.

**Not carried out.** The readiness gate (N60) and the M8 configuration (`config/experiments/m8-calibration.yaml`)
remain in the repository as preparation. No reported result uses them.
