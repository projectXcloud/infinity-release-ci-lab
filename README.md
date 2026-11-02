# Infinity release CI laboratory

Public **synthetic fixtures only**. No ProjectX product source, customer data, or
credentials. Default branch: `dev`. Stable integration branch: `main`.

Source versions are immutable Git tags, `vX.Y.Z`, allocated after merge. Untagged
feature commits are normal. A PR requests a version; the publisher selects the
next unused patch on that major/minor line. An occupied tag is never moved.
Hotfixes merge from `main` back into `dev`; tagged commits are never rebased.
GitHub merge commits get their own source identity.

Every release PR includes exactly one block:

<!-- release-request -->
```json
{"component":"base","requested_version":"1.0.1","release_kind":"source"}
```

Components: `base`, `app-a`, `app-b`. Kinds: `source`, `integration`, `promotion`.
No `VERSION` files. Track the example config; ignore local/generated config and
dependency caches. CI snapshots `IMAGE_BUILD_CONFIG_DEV` or
`IMAGE_BUILD_CONFIG_MAIN` without falling back to the example. Variable changes
require an explicit reconcile dispatch.

Tiny Linux/amd64 OCI images use GHCR. Base uses `FROM scratch`; apps use the exact
published base digest. Paths separate `dev` and `main`. Candidates use full
material hashes; release images use `A.B.C`. The `release-ledger` branch records
config snapshots, sources, inputs, reservations, verified digests and selections
using Git compare-and-swap. Publication and ledger commits are separate: retrying
the same request resumes its reservation. Gaps/orphan candidates are allowed;
overwrites are rejected.

This lab does **not** prove cross-repository authentication/dispatch, GCP Artifact
Registry permissions/immutability, or Cloud Build execution. GHCR immutability is
a publisher policy, not server-side enforcement. Production needs a trusted
publisher and reviewed workflow changes. This lab deliberately requires no
second reviewer so its owner can exercise real PRs.

Local checks: `uv run --no-project python -m unittest discover -s tests -v`.
Live results and limitations will be recorded after actual workflow runs.
