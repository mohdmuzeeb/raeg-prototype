# Commit SHA map (author-name correction before first push)

Before the first push, the two initial commits were rewritten on 2026-10-11 to correct a malformed author name ("“Muzeeb" became "Muzeeb Mohammad"). Only commit metadata changed. The git **tree** of each commit, which is the exact file content, is identical. Test manifests created before the rewrite therefore reference the old SHAs. Resolve them with this table:

| Old SHA (in manifests) | New SHA | Tree (identical before and after) |
|---|---|---|
| `fc8b0fa028d2b41fb3b782037204868c63ae6c3d` | `aebe59dec9d4ff768c4331510ac1c7dd4d9c1896` | `19106b1791f8aab0619f6ab93d84c883ed864931` |
| `41e2159` (never referenced by a manifest) | `936d7c980dff56315bd8fa6721a4bf746555c20c` | `3c727a3a1bf0f6c704ade80949781232aef59656` |

Check: `git rev-parse <new-sha>^{tree}` gives the tree in the last column. Each manifest's `source_tree_sha256` covers the source files independently of git.

# Commit SHA map (test-log sanitisation before the M7 push, 2026-10-11)

The seven commits made after the last published commit `3f15ae3` were rewritten locally before their first push. Published history was not rewritten, and nothing was force-pushed.

**What changed.** Only tracked test logs under `results/tests/` changed, by text substitution:

| Original text | Replaced with |
|---|---|
| the absolute repository path | `<repo>` |
| macOS pytest temporary paths | `<tmp>/pytest-of-<user>` |
| the home directory | `~` |
| the local username | `<user>` |
| the machine hostnames | `<host>` |

No file outside `results/tests/` changed in any commit (verified per commit with `git diff <old> <new> -- . ':!results/tests'`). Commit messages, authors and dates are unchanged.

**Scope.** In the rewritten commits, the substitution was applied to every tracked test log, including logs first published in earlier commits. The new tip therefore contains no local identifiers. Copies of the older logs in the already-published history (up to `3f15ae3`) are unchanged.

**What this means for manifests.** The run manifests, the test manifests and every archive under `results/archive/` are unchanged. They still reference the **old** SHAs, so resolve those with this table:

| Old SHA (in manifests and archives) | Published SHA | Published tree |
|---|---|---|

**Code identity of the M7 pilot.** All 280 pilot manifests record `git.head = 235ffd00124b…` and
`source_tree_sha256 = 804d292c10e1509f4e511ac7ca118701d671055984e20f0384ba768406de7656` (102 source files). Recomputing
`harness.manifest.source_tree_sha256()` in a checkout of the published `6c7a2a7` gives the same value. The published
commit therefore contains exactly the code that produced the dataset.
