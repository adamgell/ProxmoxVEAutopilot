# OSDeploy fixed-stage and immutable-input contract acceptance

Accepted locally on macOS ARM64 and Linux AMD64 on 2026-09-05. This is a pure contract milestone, not a rebuilt controller service, executed OS deployment or production-readiness decision.

## Source and reviewed behavior

- Task1 source: `91976025ff1bbfec4ad42680f3599a7a97272b4d`.
- Complete phase source: `6ee5bf08eed76b20e17056df9b2d27bbc1581113`.
- Plan: `docs/superpowers/plans/2026-09-05-rust-osdeploy-contract.md`.
- Evidence directory: `.superpowers/sdd/2026-09-05-rust-osdeploy-contract/`.

The crate defines sixteen fixed stages, distinct PE/disk starts, the sole explicitly guarded shutdown-escalation dependency, grow-only exact disk capacity, deterministic requested/PVE/Windows/agent names, pinned phase policy and a complete immutable deployment snapshot. The snapshot binds all nineteen inputs plus sixteen fixed contract fields into canonical SHA-256. It preserves artifact source/output/apply-index provenance and applies the signed guest index limit only to the effective apply index. Storage-free minimum and requested/effective disk capacity remain independent.

Output construction is private and validated; no unchecked deserialization or execution capability exists. Optional payload hashes remain declared or unverified, never verified. References, selected template hash, firmware/media policy and force-stop flag confer no observation or dispatch authority. The independently computed complete fixture digest is `47035f6731b8a27dceabbe9fb2ff63aa952204dfa3b6420d94a9a35c1fe3e5a9`.

Task1 and Task2 each received fresh Astra specification/quality review. A separate final reviewer read the entire combined 80,448-byte / 2,154-line package and approved integration. All three reviews were findings-free; no repair wave. Main read complete implementation reports and reviewed evidence limits. Reviews did not rerun unchanged tests.

## macOS proof

At the complete source, **31 runtime tests and six compile-fail doctests passed** (12 input-values, 15 plan, four stages). Behavioral RED was recorded separately from missing-API compilation for each task. The Task2 report records one corrected fixture-length typo without weakening policy. Full workspace formatting and strict all-target/all-feature Clippy passed. Cached offline dependency policy passed with five inherited duplicate/unused-allowance warnings; no dependency or advisory refresh occurred. No unrelated PG/Compose suites ran for this pure phase.

## Exact Linux proof

The focused offline, locked, network-none Linux build executed only osdeploy-adapter tests: **31 runtime plus six compile-fail doctests passed**, zero failures/ignored/filtered. Subsequent no-run discovery did not add executions. All four harness paths match independently inspected x86-64 Linux ELF binaries and hashes.

| Identity | SHA-256 |
| --- | --- |
| 147-input source manifest | `e53705d48a79cf0f2d17f3b81a07857399ec789d3a7346bdd4aed1acf449ecd0` |
| Proof image/index | `17b440b205f34022f5749641ea164462e9785a4681415565e8a800bf6b8f7f64` |
| AMD64 manifest | `086fe60c08227a874eff4ca48beaa93be960d573a940bc21bc6189dda17b19e3` |
| Config | `746eb68f6255f2bdcf4ad9216eb5957924ca38519d4a70615213c2fdb8f72864` |
| 23-file evidence manifest | `6ab285550d4759a44b369c384e7619ee24cc5935d9b20dd92bd34a68dcda846b` |

Fresh staging contains exactly 148 regular files (147 tracked inputs plus manifest), no symlinks or extras. Main independently verified every source checksum and all 23 sealed proof files at the frozen source, read the complete Linux report/recipe, and checked test-result, ELF, metadata and cleanup evidence. Proof tag `rust-controller-osdeploy-contract-proof:6ee5bf0` uses a shell/message entrypoint. Cumulative image size is 7,258,841,712 bytes across 181 layers, not unique disk consumption.

**The inherited service remains c429807443f80b74530cbd10af334e50e97b695b**, unchanged binary SHA `cb8414af170df9ec6276476f19632424d574e52c463cfa41a9c3a23403cb8fd8`. It was not rebuilt or run. The accepted Compose alias remains the selected-node service image, not this library proof.

Both exact owned inspection containers are gone. Pre-existing container/image/network/volume inventories were preserved, including all four historical stopped containers. No pruning or cache cleanup occurred. Final free space was 119,637,000 KiB, above the 18 GiB guard. Build RUN networking was disabled; no daemon-wide disconnection claim is made. No live infrastructure, secrets, publication or deployment was involved.

## Remaining scope

Next is a sibling rich PVE provisioning contract and fake execution, preserving the older three-stage behavior. Durable registration/reload, reservations, parked waits, callback/guest-action binding, controller/service composition, actual restart/fault proof, real PVE/Windows behavior and all other PoC tracker gates remain pending. A semantic configuration hash cannot prove blank template contents, media publication or OS readiness. Production and real PVE remain read-only; non-production mutation and cutover need separate approval.
