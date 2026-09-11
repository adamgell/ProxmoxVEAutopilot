# Selected-node service acceptance

Accepted locally on 2026-09-05 for executable source **c429807443f80b74530cbd10af334e50e97b695b**. This documentation commit is distinct from the artifact's embedded source revision. This closes the bounded selected-node observation phase, not the production-candidate PoC goal.

Explicit singleton node selection, narrow node/network collection, independent scheduling, per-component freshness and sanitized additive health are implemented. Cluster policy remains 3-second collection / 5-second cadence / 15-second freshness; infrastructure uses 2-second component and 5-second pair bounds, 10-second Skip cadence and inclusive 30-second component freshness. Historical success cannot make current failure ready. Authenticated HTTP observation never grants execution readiness and retains HTTP 503. One protected credential load precedes worker construction; selection does not enable storage, QGA or mutation capabilities.

Both independent Astra task reviews and the consolidated 2,672-line review were clean. No repair wave was needed. Main read the final artifact report, actual readiness and complete Compose logs, and independently verified all 21 evidence hashes.

## Verification and limits

- macOS Task 2: 442 runtime + 19 documentation = 461 top-level executions reported, fmt, strict workspace Clippy, eight Python checks and cached offline dependency audit passed. Exact-source post-commit actual service proofs passed. Shared/nested repetition is documented in the task report; totals are executions, not unique test definitions.
- Linux AMD64: 256 selected build-runtime executions + 16 actual Compose adapter executions = 272 runtime; 12 compile-fail doctests give 284 top-level Rust executions, plus eight Python tests. Five infrastructure-health tests run under two filters; 20 shared cross-consumer repetitions are included; four nested PVE proxy reruns are excluded. Full workspace all-feature targets compiled, not all executed.
- Actual Linux selected-node and cluster services with unavailable database passed, including exact SHA and sanitized false execution readiness. Both actual HTTP-plus-PostgreSQL no-write service tests remain **macOS-only**; both were explicitly excluded on Linux. Native fake-controller example is Linux compiled-only, and native-controller PostgreSQL E2E is not established on Linux by this gate.
- Separate actual release fake observer returned HTTP 200, exact SHA, fake/synthetic labels, database/outbox true and one successful sweep, with empty mounts. This is fake health, not authenticated execution readiness or device proof.
- Unmodified multiworker Compose proof: three ready workers, three claimers, concurrency cap two, maximum one attempt per operation, four satisfied / two unknown / one pending, 39 sweeps, real 30-second lease expiry. Adapter unit 12 and native PostgreSQL four passed. Both exact owned projects were cleaned; four unrelated old stopped containers and all predecessor evidence were preserved.

## Artifact identity

| Item | SHA-256 |
| --- | --- |
| Image/index, 160 layers | aa3bd3e7b2f6b929e1a82404d9907f787f23f7ab64cc7b2965babb81200bce68 |
| Linux AMD64 manifest | 7e27b584b5ec6c3a95bdb123139d59e4fa5c7bc33441be954c6acb6d2e78e5de |
| Image config | 4cf18f0637f36ce48593c51da1138bdb5eea173c74b0ca341c24341f5c74e6e7 |
| Release service ELF | cb8414af170df9ec6276476f19632424d574e52c463cfa41a9c3a23403cb8fd8 |
| 130-input source manifest | d0a64b575bfbd14974e3970761dc12635a89dd2c0892395b48968261ba1b31b9 |
| 21-entry evidence manifest | eda327ef98e2aef330f3fdb5f15b48ebf1f72a2b576cbe04918462485c597d05 |

Exact fresh COPY context has 131 regular files (130 tracked inputs plus manifest), no symlinks/extras, isolated absent Python cache prefix, and excludes the new goal tracker. Cached parent is accepted node/network image 820eee. Locked offline Cargo, network-none build RUN, no dependency pull, streamed image metadata without host tar archives. Docker daemon network isolation is not independently claimed. Final image cumulative size is 7,161,234,273 bytes, not a host-reclaimable-space figure.

Evidence resides in `.superpowers/sdd/2026-09-05-rust-selected-node-service/`, including sealed report, manifests, recipe, reviews and logs. These local ignored files are not published artifacts. Cached advisory evidence remains Task 2's 2026-09-02 advisory revision; no refresh was performed by the artifact gate.

## Remaining programme boundary

No real Proxmox, production controller, tenant, credentials/vault, SSH, deployment, publication, merge or device acceptance action was performed. Disk headroom recovered before cleanup; no Rust-cache cleanup or Docker prune was necessary. Production remains untouched.

Next is the pure artifact identity/byte-match contract, followed by OSDeploy input and callback/native workflow work. Native service execution, retained client families, actual dual-executor fencing, compatibility projections, restore/rollback and independently approved non-production proof remain open in `poc-readiness-tracker.md`.
