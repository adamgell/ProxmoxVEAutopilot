# Artifact identity and supplied-byte contract acceptance

Accepted locally on 2026-09-05 for pure library source **15fadd049ee7a52ea61a2edfe6789a15a6929628**. This documentation commit is distinct from the tested source. No newly built controller-service artifact or production readiness is claimed.

`artifact-index` provides a validated immutable declared descriptor, canonical fingerprint, explicit source/output image-index provenance, and independent ISO/WIM supplied-byte hash comparison. Private outputs have no Deserialize or unchecked public-field construction. Descriptor evidence is always `declared_only`; matching bytes produce `supplied_bytes_matched` with `publication_verified=false`. Neither grants location, filesystem, WIM, boot, VM mutation or workflow authority. Legacy APIs and the existing service are unchanged.

## Verification

- macOS: 18 runtime tests plus three compile-fail doctests passed. Tests distinguish scaffolding compile failure from behavioral RED. Removing either ISO or WIM comparison independently caused its mismatch test to fail; both comparisons were restored before final GREEN. Format, strict all-target/all-feature workspace Clippy and cached offline dependency policy passed. Existing duplicate-family/unused-license warnings and 2026-09-02 advisory-cache date are disclosed in the implementer report; no fresh advisory fetch is claimed.
- Independent Astra task review and final integration review each read the complete 927-line change package and were clean. No repair wave required. Only nine planned source paths changed; Cargo.lock adds the local crate only, existing external versions and Docker test filters are unchanged.
- Linux AMD64: all 18 runtime tests plus three compile-fail doctests passed on exact-source input. Three test executables were identified through Cargo output, verified as x86-64 ELF and independently hashed. No unrelated tests or service/Compose/PostgreSQL execution was added to this library-only gate.
- Main read the final report and relevant build/inspection/cleanup logs and independently verified all 12 sealed evidence hashes. Fresh context contained exactly 138 tracked input files plus manifest, with no extras or symlinks. Offline locked Cargo, network-none build RUN and verified cached local parent; no dependency pull/fallback. Docker daemon-wide network isolation is not claimed.

## Exact proof identities

| Item | SHA-256 |
| --- | --- |
| Library-proof image/index | 21253cbec331f119c9c447520672737f449ba48e080c3493d8ff2038a8261a41 |
| Linux AMD64 manifest | b9acc953997810f2e9993107898e6aef4e468a19e67a581e43c1dde702d0dc3e |
| Config | 882c900632f43b8759fa8b6f31cfdf2809cb475e8ba8b0df3f8edfff20e11ff7 |
| Source manifest, 138 inputs | 0623eea5e6aa3747112d6548081d87250a96cc907ec3f33e244a9478f52b5f42 |
| Evidence manifest, 12 entries | 627c3e8f2b174bd98539dabf5111bf898a860039b2504a7014b112d8fe7a04d2 |
| Byte-test executable | 63cbde7a1488b1916d0ba3c61bda06e28af5c6811a93e397d26f8bcff3ecaa37 |
| Descriptor-test executable | 9424ee835ea8570cb5cc2583915ec1843b3715d09feb143b12aec2a96b155d0c |

The distinct library-proof image uses a shell/message entrypoint. Its inherited service binary remains **c429807** with SHA-256 `cb8414af170df9ec6276476f19632424d574e52c463cfa41a9c3a23403cb8fd8`; it was neither rebuilt nor run. The accepted Compose alias was not retagged. Later service integration must produce its own exact-source executable and workflow proof. Image cumulative size is 7,197,350,961 bytes / 171 layers, not a host-reclaimable figure.

Detailed local evidence: `.superpowers/sdd/2026-09-05-rust-artifact-contract/`, including both reviews, task report, frozen manifests and Linux logs. These ignored local records are not published artifacts. No archive exports or broad cleanup occurred; the exact owned inspection container is gone, and four original stopped containers and predecessor evidence remain intact.

## Open programme obligations

OSDeploy input binding must resolve applied WIM index against output-image metadata and the existing client's signed 32-bit index range; the generic metadata u32 range is not client execution assurance. Legacy source-image labels do not prove source provenance across build engines. Disk capacity, free-space requirements, identities, firmware/media and payload contracts remain separate workflow inputs.

The service-driven two-boot workflow, durable callback waiting, typed native extensions, real-wire ownership proof, retained client families, shared Python fencing, readiness projections and restore/rollback remain open. Production and real Proxmox remain read-only; live mutation trials, deployment and cutover require separate approval. RustedOutClient stays excluded. The active PoC goal is not complete.
