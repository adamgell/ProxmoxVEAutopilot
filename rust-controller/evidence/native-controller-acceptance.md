# Local native fake-controller acceptance

Accepted executable source: `0f4c48bea66514c6d35a209d3a5a335c4d4b3e5a`.
Date:2026-09-05. Platform: macOS ARM64 development and Linux AMD64 execution through local OrbStack.

All six native-plan task gates passed independent Astra review. The final whole-phase review found two P2 issues: permanently aging fake infrastructure timestamps and unbounded/not-pre-owned Docker lifecycle in the native store fixture. One coordinated repair added regression proof and shared private bounded lifecycle handling. Independent scoped re-review closed both with zero remaining findings.

## Accepted capability

The local library executes one bounded, fake-only clone/configure/start workflow with immutable plans, fixed resource identity, durable reservations/dispatch/receipts, locked-store decisions, historical clone ownership, cancellation and no-send Unknown reconciliation. The executable proof measured three mutations, three satisfied operations and one reservation, ending with a running fake VM. It explicitly reports `os_readiness_proven=false`.

Real native HTTP mutation and native service activation remain unavailable. Fake provenance is not a claimed PVE wire feature. This does not replace the existing production controller or Ansible operation families.

## Executed verification

- Final repair macOS full workspace:308 runtime tests plus11 compile-fail doctests passed. Nine shared lifecycle scenarios execute in both native fixture consumers; the319 top-level results are not319 distinct behaviors. Formatting, strict all-target/all-feature Clippy and standalone fake proof passed.
- All115 selected source inputs were checksum-verified on the host, during Linux build and in an independent final-image inspection. The obsolete cached helper was hash-verified then removed only inside the disposable image; both new shared support files were included.
- Linux release workspace examples/service and adapter registry verifier passed; all workspace/all-feature test targets compiled.
- Selected Linux execution:128 runtime invocations plus4 compile-fail tests, representing123 distinct scenarios after excluding nine duplicated shared lifecycle invocations. Eight Python tests also passed. This is not full Linux workspace execution.
- Actual multiworker Compose proof:three ready workers, two claiming workers, cap2, four satisfied/two unknown/one pending, maximum one attempt per operation, real30-second lease-expiry recovery. Linux adapter12 lifecycle/process and4 native PostgreSQL tests passed.
- A separate owned observer-only replay verified the actual running image and `/readyz`: exact accepted Git SHA, ready=true, observe mode, fake transport, synthetic/no-device-readiness evidence and positive sweep count. This was separate-run evidence because the initial health capture missed the completed multiworker project's lifetime.
- Both exact Compose projects and native-proof ownership inventory were empty after cleanup. Unrelated resources were left untouched. Reusable local images/caches remain.

Final image/index: `sha256:f49f0e244d9cf0e760901c6dc710f835d3defe3bffac1eff204dda221ba19df6`.

Linux service SHA256: `4faed07b1266560e1af13a073cf8fc7f8b1cbe44cc129874fe88182b2202d9c0`.

Source-manifest SHA256: `fa2896efa7eed02051d02f90ba259caa3db1229eb96c65c85ce70454890690fc`.

Full commands, reports, recipes and logs remain under `.superpowers/sdd/2026-09-04-rust-native-pve-controller/` in the isolated worktree. Main independently verified all20 files in `final-linux-evidence.sha256`. Historical foundation and task6 artifacts retain their original revisions.

## Qualifications and remaining programme

One initial digest-style Docker FROM reference triggered a denied public registry metadata lookup before any image pull or RUN step. A verified content-named local parent tag then built successfully with locked offline Cargo and network-none RUN stages. This is not a claim of completely disconnected tooling.

Native-controller PostgreSQL end-to-end ran on macOS, not Linux. Linux compiled the native example and database tests; its executed database proof was the separate adapter/Compose scenario. No new cargo-deny advisory assessment ran after this repair: the unchanged prior policy/lock result passed with documented duplicate and unused-allowlist warnings.

No installed PVE-release compatibility, real mutation, OS installation, OOBE/enrollment/ESP/usable endpoint, hosted CI, publication, deployment or production readiness is established. Production `192.168.2.4` and real PVE remain read-only; RustedOutClient stays excluded.

Next Rust slice is bounded authenticated GET-only observation, followed by infrastructure/artifact expansion, separately reviewed real-write provenance/retry/reservation policy, media/firmware/TPM/QGA, OSDeploy/agent/CloudOSD migration, shared Python/Rust fencing and restore/fault proof. Disposable live mutation and production cutover remain separately approved gates. Deferred product tasks resume only after relevant controller contracts stabilize.

This acceptance document is added after the executable-source commit; its documentation commit is not the embedded artifact SHA.
