# Experiment log

Raw results are archived locally (not in git). One row per archive. The git HEAD column holds the SHA recorded at archiving time; see `docs/COMMIT_MAP.md` for published SHAs.

| UTC | archive | sha256 | runs | files | size (bytes) | git HEAD | note |
|---|---|---|---|---|---|---|---|
| 2026-10-11T05:03:40+00:00 | `results/archive/pilot-calibration-20261011.tar.gz` | `4393be99c73f501c16e2b9a7af69b5a8176880dbf0fc882367b1e689b169fe50` | 12 | 1451 | 3949536 | `ccc9314c2bb9` | M7 timed calibration batch (12 runs, seed 101); excluded from paper |
| 2026-10-11T05:59:49+00:00 | `results/archive/pilot-recalibration-20261011.tar.gz` | `4fb0f0989778fa197784570cc9ed05ff0f1ab401465a0a404581c77c25206f6e` | 20 | 2433 | 4945665 | `8bee26344d9a` | M7 short re-calibration after PC-1..PC-4 (20 runs, seed 102, commit 8bee263); one run spans a host sleep (N55); excluded from paper |
| 2026-10-11T14:58:32+00:00 | `results/archive/pilot-timing-20261011.tar.gz` | `df79943aca0a144eb34a32debf4994228e2100677a5da47b7ee88e29a373d1b5` | 4 | 494 | 1189958 | `3f58f6cf2466` | M7 Option A bring-up timing batch (4 runs, seed 103, commit 3f58f6c); projection 3.14 h; excluded from paper |
| 2026-10-11T18:10:04+00:00 | `results/archive/pilot-20261011.tar.gz` | `66da0511c5879d988e49d1915461fdd3c29ab8744afde50253351d62192ac23a` | 280 | 29988 | 72765479 | `235ffd00124b` | M7 pilot: 280 runs (252 + 28 FC), seeds 1-3, commit 235ffd0, all complete, all audits pass; wall clock 3h06m; excluded from paper |
| 2026-10-11T18:10:16+00:00 | `results/archive/pilot-derived-20261011.tar.gz` | `8498e4da711d34c537289e149d4a11d7251cea3408e7cf9455ca7a67cfa51c50` | 280 | 15 | 43226 | `235ffd0` | M7 pilot derived analysis (runs.csv, safety.json, precision.json, summaries, projections, M8 cost model, runner stdout); regenerable from the raw archive |
| 2026-10-11T18:34:11+00:00 | `results/archive/m8-calibration-stopped-20261011.tar.gz` | `c894441ac6f645872e0d84036f9663f7f4a33e549ec5121c43b509cf4889336a` | 3 | 544 | 1016671 | `370a29e7c13e` | M8 calibration STOPPED by user decision (experimentation ends at M7): smoke run s900 (uncommitted code), 1 complete run s104, 1 run interrupted mid-run (manifest status 'running'); excluded from paper |
