# Owned Linux fixture and registration runtime acceptance

Accepted locally against source `f412f2c9d577081985ce503773dfa8226567f099`, containing reviewed fixture source `a36c166f373331ff0a66324fd9301d6a745c5721` and registration source `53272397cdd386ec05b7ac6332780ba9ef15e62d`. This closes the four-package Linux database/runtime prerequisite, not a new executable controller service or production readiness.

## Exact-source runtime

Fresh session `072c09397f6c469eaa3a515017f8676b` admitted a clean worktree and 192 tracked inputs, retaining exact hashes and the original 221 ordinary baseline names plus ten documentation cases. Build used cached images, offline/locked Cargo and disabled networking. Immutable inspection and before/after runtime checks matched source and prebuilt Linux/amd64 executables. Runtime used all four packages with `--include-ignored --test-threads=1 --nocapture`; unexpected non-documentation rebuilds or missing/changed names were refusal conditions.

| Harness | Passing executions |
| --- | ---: |
| operation-controller decision / postgres_native | 24 / 59 |
| osdeploy-adapter input_values / plan / provisioning / restore / stages | 12 / 15 / 3 / 14 / 4 |
| postgres-store native / osdeploy_registration / postgres | 64 / 50 / 55 |
| scheduler postgres | 61 |
| osdeploy-adapter / postgres-store documentation | 6 / 4 |

Total: **361 ordinary + 10 documentation = 371 Rust executions**, zero failed/ignored/measured/filtered in every harness. Four empty library harnesses and two empty documentation harnesses were accounted for. Nine Linux ownership cases ran in five shared harnesses (45 included executions). Seventeen Python checks passed separately. Shared repeats mean totals are not distinct test-body counts. Compile-fail documentation diagnostics were expected and their harnesses passed.

## Database ownership and preservation

Fresh PostgreSQL used network none, fixed loopback identity, 100 connections, 512 MiB PGDATA tmpfs and 1 GiB memory. The runner shared only that namespace and a read-only receipt, with no Docker socket or writable host source/target mount. Host verification matched all 25 intentional refusal databases and rechecked instance identity. Final PGDATA free space was 290,910,208 bytes; no refusal database was deleted to obtain a pass.

After preservation, exact full-ID/label/image checks and final logs, only the owned runner, inspection and PostgreSQL containers were removed. Tmpfs database contents are not recoverable; catalog, logs and evidence remain. The final inventory preserved four original containers, three networks and 4,220 volumes. Only the admitted candidate and parent-alias image tags were added. No prune, volume removal or Rust cache cleanup occurred. Final host free space was 38,168,692 KiB, a historical observation.

## Artifact evidence

Main independently reran the separate verifier: **27 local synthetic checks passed in 11.886 seconds**, including twelve real hung workers, exact descendant absence and completed reaping. The earlier independent 23-pass/one-error signal-EPERM run is preserved, followed by deterministic regressions and repair. Permission denial remains uncertainty, never absence proof.

Actual export preserved 163 allowed paths (772,247 payload bytes); main independently checked all 503 export-seal files. Parent and final OCI checks independently hashed actual index, selected manifest and config bytes, checked descriptor sizes/linkage, Linux/amd64 configuration and source variables. Main checked all ten OCI-seal files. Layer descriptor shapes/order/count were checked, not layer contents, signatures or provenance.

- Final index: `sha256:7aec4fa9cd159e52c2eabc97ebc5fbd91f7a06d8692849cbaf3b044612af5f57`.
- Final manifest: `sha256:eae1cb97d3a4f8b621912abef344d8231475c9dff64e83678a9f0da553f00caf`.
- Final config: `sha256:bc4def6ad092b39a777d50990e3caf968ee120c06115a61dc7ff2b4c2b711825`.
- Tag: `rust-controller-linux-fixtures:f412f2c9d577-072c09397f6c`.
- Independently rechecked parent index: `sha256:83ea9e9863ae75fefabbb1d15faa2c003948b672a4ee714de19eb72f011e19de`, with its previously accepted manifest/config.

Both images retain historical service source `c429807443f80b74530cbd10af334e50e97b695b`; its service binary was unchanged and unexecuted, not relabeled as this test source. Raw OCI config/environment was not persisted.

Final cleanup exited zero. Main independently verified **2,534 sealed files**, including both additional manifests and every listed hash. Final seal SHA-256: `f631ccb36a4d019ee78711962866b78d718285642a61bcaf20133df6a5dad53b`. Local proof directory: `.superpowers/sdd/2026-09-05-rust-linux-owned-fixtures/candidate-runs/072c09397f6c469eaa3a515017f8676b/` in the isolated design worktree.

## Remaining goal

Earlier unexplained native macOS failures/interruption remain release-assurance work; passing corresponding cases here does not establish their cause. No final full post-supervisor-fix macOS suite is claimed. Durable OSDeploy execution, compatible callbacks/guest behavior, the complete sixteen-stage service workflow, independent-process restart/fault/recovery, single-writer transition and production-candidate assessment remain outstanding. Real Proxmox and production `192.168.2.4` remained untouched. Deployment, external mutation and cutover require separate approval.
