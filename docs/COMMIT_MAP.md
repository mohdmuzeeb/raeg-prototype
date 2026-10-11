# Commit SHA map (author-name correction before first push)

Before the first push, the two initial commits were rewritten on 2026-10-11 to correct a malformed author name ("“Muzeeb" became "Muzeeb Mohammad"). Only commit metadata changed. The git **tree** of each commit, which is the exact file content, is identical. Test manifests created before the rewrite therefore reference the old SHAs. Resolve them with this table:

| Old SHA (in manifests) | New SHA | Tree (identical before and after) |
|---|---|---|
| `fc8b0fa028d2b41fb3b782037204868c63ae6c3d` | `aebe59dec9d4ff768c4331510ac1c7dd4d9c1896` | `19106b1791f8aab0619f6ab93d84c883ed864931` |
| `41e2159` (never referenced by a manifest) | `936d7c980dff56315bd8fa6721a4bf746555c20c` | `3c727a3a1bf0f6c704ade80949781232aef59656` |

Check: `git rev-parse <new-sha>^{tree}` gives the tree in the last column. Each manifest's `source_tree_sha256` covers the source files independently of git.
