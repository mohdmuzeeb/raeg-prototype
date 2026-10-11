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
