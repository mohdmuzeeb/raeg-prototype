# Implementation notes and deviations from DESIGN.md

These are recorded as they arise. Each entry gives the milestone, what differs from DESIGN.md, and why.

## M0–M2
| # | Deviation / clarification | Reason |
|---|---|---|
| N1 | K2 clarified: a serialize-mode waiter replays the record it waited on, even if `created_at + τ` passed while it waited. | Bug found by conformance test KB6 (run `20261011T012239Z_conformance`). The fix is recorded in DESIGN §2.4. |
| N2 | Provider faults are keyed by `(client_reference, kind, provider ingress index)`, not by `(k, attempt_no)`. | Baselines do not number their attempts consistently. Crash and pause hooks keyed by `(k, attempt_no)` live in the shared client. DESIGN §6.3 will be updated when the fault-plan generator is built (M6). |
| N3 | CP2 fires `after_send_ms` after the request is handed to the transport. | A client cannot observe when the bytes reach the wire. |
| N4 | The oracle is protected by a token and a separate port, not by network isolation. | Simpler with a single Compose network. Systems under test never receive the token. |
| N5 | The colima VM clock is offset from the host clock (≈1.5 s observed). | Every service shares the VM clock. Host-side code reads the provider clock. |
| N6 | The provider is a single process, and its ingress counters are in memory. | Determinism of fault matching. After a provider restart, counters and the fault plan reset (see the K7 restart test). |
| N7 | `X-RAEG-Attempt` is sent by every system through the shared client. | Uniform evidence. The header name is kept from the design. |

## M3 (gateway store)
| # | Deviation / clarification | Reason |
|---|---|---|
| N8 | T16 is split into `T16s`, `T16d`, and `T16n` (one id per target state). | Each transition id has exactly one target, which simplifies auditing. |
| N9 | New append-only table `submissions` records every T1, T1a, and T1b request. | DESIGN §3.2 defined T1a and T1b without any record. A rejected fingerprint conflict is evidence too. |
| N10 | **Rejected** requests also write a `decisions` row (`applied = false`, with the reason: `FENCED`, `VERSION_MISMATCH`, `STATE_MISMATCH`, `LEASE_NOT_EXPIRED`, `NOT_DUE`). | "All recovery decisions preserve raw evidence." I6 is checked on applied decisions only. |
| N11 | The fencing token is the granted `lease_expires_at` (unique per grant), plus `attempt_no` for dispatch leases. | A worker id alone cannot distinguish two grants to the same worker. |
| N12 | An owner whose lease was swept receives a recorded `FENCED` rejection, not an exception. | Losing a lease is a normal race, not a programming error. Illegal transitions requested from a correct snapshot still raise `IllegalTransition` before any write. |
| N13 | All store times use the database's `now()` (transaction start). | It is never later than any action taken after commit, so anchors and deadlines are conservative (DESIGN §2.6). |
| N14 | `executions` has column-level UPDATE grants. Identity columns (`action_key`, `fingerprint`, `payload`, `profile_id`, `capability`, `policy`, `created_at`) cannot be updated by `raeg_app`, and no row can be deleted. | Defence in depth beyond the patch whitelist. |
| N15 | The event hash is `SHA-256(prev_hash ‖ JCS(body))`, where the body holds the event fields with `at_db` as integer epoch microseconds and seq 1 has `prev_hash = 0…0`. | Exact, platform-independent re-computation from exported data. |
| N16 | Retry guards are passed to `claim()` as a callback and evaluated inside the claim transaction against the new deadline. | Implements the atomic re-check of T13 without the store depending on the controller (M4). |

## M4 (recovery controller)
| # | Deviation / clarification | Reason |
|---|---|---|
| N17 | New same-state transition `TW` (UNKNOWN → UNKNOWN). Its patch may only set `next_action_at`. | VB1's `WAIT` needs an audited, hash-chained record without a state change (DESIGN v0.3.1 §0b E2). |
| N18 | New same-state transition `TA` (SUCCEEDED → SUCCEEDED). Its patch may only set `duplicate_detected`. The audit's I5 check allows it. | Rule DS duplicate audits and late-success duplicates annotate an absorbing state without changing the claim. |
| N19 | Late evidence is consumed by the engine *before* `decide()` runs, not inside it. Consumption = a decision (applied, or a no-state-change note) that cites the evidence id. `executions.pending_late_success` is unused. | Keeps `decide()` pure (DESIGN v0.3.1 §0b E3). |
| N20 | `store.note_decision()` records decisions that change no state (`applied = false`, `output.noop`): late evidence that is not a success, `LS-CONSISTENT`, anomalies for terminal or escalated actions, clean duplicate audits. | "Record every recovery decision", including the ones that change nothing. |
| N21 | **Design correction: B1 now bounds the commit time, not the arrival.** | The model checker found Tier B duplicates under the v0.3 wording (`tests/property::test_b1_as_worded_in_v03_is_insufficient_without_d`). DESIGN v0.3.1 §2.7 and §11.2 are updated. Tier S is unaffected. |
| N22 | **Bug fixed:** the store judged transition legality before the CAS version check. A worker with a stale snapshot therefore got `IllegalTransition` instead of a recorded `VERSION_MISMATCH`. | Found by the three-engine concurrency test (run `20261011T031544Z_integration`). The version check now comes first. |
| N23 | L-EU (absence unprovable) is applied as T11 with `escalation_reason` set; `decide()` then escalates via T14 (`R-E-PENDING`). The same mechanism carries `INTERNAL` (K4 mismatch) from T5. | Implements DESIGN §3.2 "T11 … L-EU (flags `escalate_next`)" without adding a column. |
| N24 | The dispatch HTTP timeout is set per request to `(d_n − now) − m`, measured after the claim. It starts when the client actually sends, i.e. after any PAUSE hook. | No send may outlive its own deadline. A zombie's late send is exactly what F7 exercises. |
| N25 | Claim decisions (T2/T13/T13a) record the store-level inputs (state, version, basis, new deadline, guard rule and detail), not the full controller snapshot. | The guard runs inside the claim transaction. Its inputs are the locked row plus the new deadline. |
| N26 | From M4 on, the test runner redacts the absolute repository path (to `<repo>`) and the home directory (to `~`) in captured logs, recording this in `manifest.json`. | Public-repository hygiene. Logs captured before M4 remain unredacted, and some are already public (see the M4 summary). |

## M5 (baselines and systems under test)
| # | Deviation / clarification | Reason |
|---|---|---|
| N27 | All systems are hosted in **one** `systems` service and selected by route (`/s/{system_id}/v1/actions`), plus one Temporal worker for every B3/B3L variant. Variants (`/noDL`, RAEG ablations) are entries in `config/systems.yaml`. | Memory on the 8 GB host. Experiments still exercise one system at a time (§8.2), so only the container is shared. |
| N28 | B3/B3L workflows always *return* a native-outcome dict (`ok`, `declined`, `error`, `not_found_after_retries`); they never end in workflow failure. | Uniform result handling and attach-to-existing semantics. The native output records `workflow: completed`, and the cause is kept in `detail`. |
| N29 | B3 workflow id = `k` with `REJECT_DUPLICATE` + `USE_EXISTING`. A resubmission attaches to the running or completed workflow's result. | The documented best-practice configuration (fair to Temporal). |
| N30 | B3 computes each attempt's deadline inside the activity (start + T_att). B3L computes it from deterministic workflow time and passes it to the activity. B1/B2 use the container clock. Every container shares the VM clock with the provider. | Transport parity with RAEG (same T_att, same header). |
| N31 | B3L treats an unanswered or unsupported lookup as "not found" and re-dispatches; a found decline record is reported as `declined`. | Conventional lookup-before-retry behaviour; there is no DD rule (as defined in §5.4). |
| N32 | Each action's stored policy carries `system_id`, and every RAEG background pass (sweeper, due actions, late evidence, audits) only processes its own system's actions. | Several RAEG variants share one store in the `systems` service. **Bug fixed:** the sweeper was initially left unscoped. Once loops started at boot (N35), the `systems` container swept other systems' leases (run `20261011T035152Z_gateway`: 3 failures). |
| N33 | Hooks reload from `/exchange-logs/hooks.json` whenever the file changes (`ReloadingHookPlan`); a missing file means no hooks. | Lets the harness arm crash and pause hooks in long-running containers without restarting them. |
| N34 | The pessimistic rescoring maps `UNRESOLVED(gave_up_ambiguous)` to a distinct `NO_EFFECT(assumed_by_agent)`, not to `NO_EFFECT(declined)`. | Keeps an agent's assumption separate from a provider-confirmed decline (§6.2). |
| N35 | RAEG background recovery loops start when the service boots. | RAEG must recover after a crash and restart even with no incoming traffic (verified by `test_cp3_raeg_recovers_after_restart_without_traffic`). |
| N36 | Temporal runs `temporalio/auto-setup:1.28.1` (pinned by digest) on its own Postgres, with SDK `temporalio==1.34.0`. | §8: Postgres persistence rather than the SQLite dev server. |

## M6 (harness)
| # | Deviation / clarification | Reason |
|---|---|---|
| N37 | **F9 redefined.** v0.3 says "hold the original before receipt (delay < d − send) so that the retry arrives first". The implementation holds it for `T_att + 1500 ms`, so it arrives *after* its deadline (rejected under D; a late arrival without D). | With the shared timing, every system retries only after the client timeout (`T_att − m`), and `d − (T_att − m) = m = 100 ms` is shorter than any retry path. The v0.3 wording therefore cannot happen. Revisit after pilot calibration. |
| N38 | F10 holds the original for `T_att + τ ± 400 ms`, choosing the sub-case (just before / just after expiry) per action from the seed. | Approximates "near the retry's record expiry" without knowing a system's retry time in advance; the receipt log shows exactly where each arrival landed. |
| N39 | V-cells combine the violation (provider-level, for the whole run) with F1 lost responses at the cell's fault rate. V1 = 3·δ lag, V3 = τ/3 retention, V4 = +20·ε clock offset. | A violation is only exercised when recovery is needed. |
| N40 | A crash hook kills the whole process hosting the targeted attempt (the `systems` service, or the Temporal worker). Every in-flight action of that system is interrupted, not just the targeted one. | This is how a gateway or worker crash behaves. It applies equally to every system, and it must be stated in the paper. |
| N41 | The per-action claim is copy 0's (the agent's primary submission). Every copy's claim and native output are kept in `claims.jsonl`. | F5 duplicate submissions model concurrent agent instances; the evaluation needs a single agent-visible claim per action. |
| N42 | The driver ends with `pending` (`UNRESOLVED(horizon)`) when either the horizon passes or `max_resubmits` transport failures are exhausted. The reason is in `native.detail`. | Both mean the agent never got a terminal answer. |
| N43 | Experiment runs start Temporal (compose profile `temporal`) only for B3/B3L systems, and run on fresh volumes with the provider reset endpoint **off**. | Runtime, and §8.2 isolation. |
| N44 | `audit.json` anchors the RAEG hash chains: it records every action's last event hash plus a SHA-256 over all heads. | Addresses the M3 risk "chain not anchored externally" for every experiment run. |
| N45 | **Harness bug fixed: crash hooks are now one-shot per armed plan, durably.** A fired `(action_key, attempt_no)` is appended (with fsync, before any crash) to `hooks.json.fired` in the shared volume, and never fires again in any process. `audit.json` adds a mandatory `hooks_one_shot` check. | In run `20261011T042207Z_m6-faults_P4-FM-r040_B1_s1`, B1 restarted its attempt numbering after each crash, so one planned CP2 crash fired **four** times (RAEG and B3 keep their attempt numbers and fired once). That broke fault equivalence across systems (§6.3). The affected `m6-faults` runs are kept as evidence and are excluded from the paper by configuration. |

## M7 (pilot)
| # | Deviation / clarification | Reason |
|---|---|---|
| N46 | Shared `client_timeout_ms` = 600 ms for every system (B1/B2 HTTP timeout, B3/B3L activity HTTP timeout, RAEG `Policy.client_timeout_ms`). It is applied as `min(client_timeout, remaining budget − m)`. | Q2 decision; DESIGN §8.4 / §0b E8. |
| N47 | F4 delay = (timeout + `T_att`)/2; F9 hold = timeout + 400 ms (< `T_att` − m). F9 is again "overtaken before `d`", superseding N37. | Fault parameters must follow the shared timing. |
| N48 | Runs record per-phase wall-clock (`bring_up`, `driver`, `drain`, `collect`, `audit`) and SHA-256 hashes of the resolved config, experiment file, `systems.yaml`, profile, workload and fault plan. The image is built once per runner invocation; every run still uses fresh volumes. | Timing measurements and configuration hashes (M7 instructions). |
| N49 | Raw results are archived locally by `scripts/archive_raw.py` into read-only `results/archive/<name>.tar.gz`, with a SHA256SUMS of every file, a `.sha256` of the archive, and a row in `docs/EXPERIMENT_LOG.md`. Archives are not in git. | Q1 decision. A curated GitHub Release comes only after verification and approval. |
| N50 | **Audit bug fixed:** `raeg_terminal` counted intents that never reached RAEG (every submission failed at the transport level while the service restarted) as non-terminal RAEG actions. They are now reported separately (`informational.never_received_by_system`). | Found in the calibration runs `20261011T045321Z_…P4-FM…RAEG-strict` and `20261011T045734Z_…P6-FM…RAEG-strict` (44 and 68 never-received intents respectively — the audit listing is capped at 20, which an earlier draft of this note misread as the count; verified from `raeg/executions.csv`: 0 actions RAEG received were non-terminal). |
| N51 | **Calibration finding (resolved by PC-1…PC-4, approved 2026-10-11):** with FM at φ = 0.2, the process-crash faults (F2, F6) cause about a dozen process crashes per 200-action run. For systems whose API runs in the crashed process (B1/B2/RAEG), these become 16–23 s outage windows, and 68–101 of 200 intents were abandoned by the agent (5 resubmits at 1 s). B3/B3L only lose their worker (0 transport errors). See the M7 status report. | Validity: FM cells would mostly measure restart outages and the agent's give-up rule. |
| N52 | RAEG runs as an API process (`systems`, records T1 and polls the store every 20 ms for the outcome) plus a recovery worker (`raeg-worker`, `run_once` loop, 20 ms idle poll). The worker dispatches PREPARED actions, so RAEG's fast path now includes up to one worker poll interval. | PC-4: worker-only crashes equivalent to Temporal's. The added dispatch delay (≤ 20 ms) applies to every RAEG action and must be mentioned alongside latency results. |
| N53 | The driver counts only resubmissions actually sent (`resubmits` = POSTs − 1), and records `accepted` per copy. | A new test caught the original counter including a resubmit that was scheduled but never sent because the horizon expired during the backoff. |
| N54 | FC-api crashes use a token-protected `POST /admin/crash` on the systems service (`os._exit(137)` after replying, i.e. the same fail-stop semantics as the hooks). Every trigger is logged to `fc_api_crashes.jsonl`. | Restart via the Docker policy, exactly like hook crashes. `docker kill` would count as a manual stop. |
