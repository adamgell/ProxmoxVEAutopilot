# Rust controller production-readiness assessment

Assessment date: 2026-09-11
Assessment source: isolated worktree `codex/rust-controller-design`  
Production and `192.168.2.4`: read-only throughout

## Current-state matrix (authoritative for this assessment)

| Area | Current evidence | Status |
| --- | --- | --- |
| macOS Rust controller prefix | Local PostgreSQL and fixture proofs through guarded EnsureStopped; strict checks recorded in the linked evidence | Proven for the bounded fixture/native-fake slice |
| Fixture-IPC stop path | Typed envelope, supervisor admission frame, PostgreSQL authority snapshot, fresh-power publication boundary, and fail-closed worker/source refusals | Contract/admission/refusal only; successful external stop remains open |
| Exact-source Linux | Source `7cde09fb` built as amd64 image `sha256:02b2be47...` in 885.14s; post-build memory guard refused owned runtime at 11.32 GiB versus 12 GiB | Build proven; Linux runtime qualification open |
| Callback compatibility | Fixture-only PeRegister/PeComplete paths are covered; generic and legacy action/result surfaces are not | Partial fixture proof; full compatibility open |
| Production readiness | Readiness artifacts and PR are draft; no deployment, cutover, or production mutation | Not ready / acceptance open |

Later entries in this document are historical source audits and retain their
original revision-specific claims. They do not override this matrix or the
current-scope paragraphs below.

Exact-source Linux evidence at `35400bd0`: the frozen amd64 archive build
passed in 826.80 seconds (image
`sha256:d615e6c8b3270ae13dc9ca95cf49aea549192b6775c1d0f1aa711457f5927f94`).
The subsequent owned full lane reached PostgreSQL admission but timed out
during bounded runner creation before controller tests began. It is retained
as an infrastructure admission failure, not a Linux qualification pass; the
launcher pin and receipts are in `requalification-35400bd0-full-1/`.

Current scope at `c78c9e9d`: this branch and PR #65 contain an accumulated
Rust controller PoC slice. The full Ansible-to-Rust port and production-candidate
acceptance remain incomplete. The fixture-only StartPe boot-arming
transaction persists package semantics, run/operation/attempt, lease identity,
dispatch linkage, and registration deadline atomically. Admission requires
the `fixture-ipc` feature and `Scheduler::with_fixture_start_pe()` opt-in;
default admission and PeRegister remain closed. The durability test covers
rollback, racing admission, reload, and missing-session refusal. Reload is
database consistency evidence, not a StartPe OS-worker death proof. Trusted
server-created fixture origins and durable fixture credential alias ownership
are implemented. The transaction-local alias helper at `724b6101` retains exact
ownership and the original timestamp on replay without independent commit or
dispatch authority.

The stop-release protocol verification recorded at `3c59f835` passes two focused
tests, formatting, strict Clippy, and whitespace checks. It validates proposal
identity/digests and classifies transport loss as ambiguous; it does not connect
physical stop dispatch or authorize a production release.

At `0c76f5f2`, three serial fixture registration integration tests also pass.
They pin fail-closed rejection for substituted VM UUID/MAC/agent identity,
wrong signing secret, expired signed bearer, competing selection, replay,
rollback, reclaim, and original-deadline behavior. The expired-bearer case is
not treated as an isolated expiry proof because it also uses an unknown alias.

At `c78c9e9d`, the physical-stop continuation audit confirms the current
consumer only records one-use outbox bookkeeping. No atomic supervisor-owned
accepted/refused/ambiguous submit outcome exists yet; connecting release now
would create a consumed/IPC-unknown split-brain retry window, so the gate
remains intentionally closed.

Commit `3e49604c` records a current-source Linux/amd64 Docker build from
`7cde09fb`: release compilation, all-feature workspace test compilation, and
focused Dockerfile layers passed in 885.14 seconds as image
`sha256:02b2be47b0f6dc726ff6e32e1beb2c25d59c2438a5572ba72ab56e699363a0ff`.
The post-build memory guard measured about 11.32 GiB available versus 12 GiB,
so the owned PostgreSQL runtime lane was refused and the launcher was not
retargeted. This is a current-source build pass and explicit runtime gate, not
Linux runtime qualification.

Commit `b89e6e43` adds an explicit supervisor-only fixture test power source:
one immutable Running/Stopped sample per StartPe identity, bound to receipt,
VM, daemon generation, expiry, and observation clock. Eight focused tests cover
install, consume, publication, reload, and stale/worker-origin/torn/conflicting
sample refusal. This source is test-only and does not replace PostgreSQL lease
authority or prove current EnsureStopped ownership. Commit `1e951d76` adds the
clock-join refusal proof: a sample older than the DB lease-check time cannot be
made admissible by repeated reads; bytes, Running state, and attempt/effect
counts remain unchanged. The required positive protocol is DB authority
preparation, an independent new power sample, then bounded admission. Commit
`fce1658a` implements that sample as a bounded checksum-linked v2 stream bound
to exact stop authority/lease identity, sequence, and predecessor digest; the
connected refresh/publication/admission/reload proof and negative replay,
supersession, owner, generation, clock, and torn-history cases pass. The
remaining seam is obtaining authority from PostgreSQL in the same orchestrated
flow and supplying a production power adapter. Commit `74cb6e3f` adds
point-in-time fixture stop-authority revalidation: it reloads locked current
owner/cancellation/lease/grace state, preserves the original sample authority
clock, and rejects changed or foreign fields. The PostgreSQL regression and
pure comparator checks pass. This is not an atomic DB-to-IPC capability; shared
supervisor transport and cancellation ordering remain open. Commit `9da7a06b`
adds `OsDeployController::admit_fixture_stop`, sequencing PostgreSQL authority
preparation, versioned sample consumption, ownership/cancellation revalidation,
committed-request checking, and typed supervisor admission. The barrier remains
entered: no Stop submission, release, Stopped publication, or DB-to-journal
atomicity is claimed. Commit `c53d8342` makes the release boundary executable:
even a valid entered EnsureStopped request returns typed
`stop_release_authority_unavailable`, remains Entered, and persists no
authorization or status across worker death/restart. This is refusal evidence;
the recoverable DB-to-IPC send/outbox and positive stop effect remain open. The
transport and PostgreSQL/native-fake tests pass, while one shared physical
StartPe/DB integration proof remains open. Commit `b615ab17` adds an immutable
supervisor stop-admission receipt carrying the persisted admission, selected
power, StartPe predecessor, stop identity, authority, and original clock.
Exact replay/reload preserves its digest and refusal responses carry no receipt.
This is supervisor evidence, not a send capability; the durable PostgreSQL
outbox, cancellation ordering, ambiguous-acknowledgement recovery, and physical
stop execution remain open. Commit `9de893f2` adds the PostgreSQL fixture stop
outbox reservation prerequisite: immutable receipt/sample selection and a
single-consumption marker with guarded-grace, grant, request, cancellation,
expiry, deadline, replay, owner, and orphan checks. Migration reapplication and
immutability tests pass. The methods remain unconnected and yield no physical
send capability; trusted supervisor-receipt integration, ambiguous-ack recovery,
and accepted-stop runtime proof remain open. Commit `9b148e0b` adds a typed
`SharedHistoryUnavailable` refusal before DB or IPC access when the controller
cannot prove that its database history and separately supplied supervisor
journal are the same operation source. Repeated and reconstructed-owner
attempts leave both pools and physical submissions untouched. This is an
explicit provenance gate, not a positive outbox integration.

Commit `432654ec` adds the corresponding positive, sealed provenance join:
the operation port and checkpoint client must derive equal opaque
operation/generation/owner/channel provenance before the controller returns a
reservation proof. This still creates no outbox row, release authority,
physical stop, or stopped-power claim; it is a bounded PoC trust boundary.

Commit `4a7d5af6` bridges that sealed provenance proof to the existing
PostgreSQL fixture stop-outbox selector and one-use consumer. The bridge still
does not release a barrier, dispatch `qm stop`, publish stopped power, or prove
replacement-worker recovery; positive end-to-end reservation evidence remains
an explicit follow-up gate.

The local verification note `local-macos-suite-20260911.md` records that the
broad all-feature workspace invocation is not a valid qualification result
under concurrent subprocess load; the affected `fixture_post_dispatch` and
`postgres_native` targets pass when rerun serially with one test thread.

The immediate unreached gate is the supervisor-owned physical-stop continuation.
`Scheduler::consume_fixture_stop_outbox` currently provides one-use exposure
bookkeeping, while `StageCheckpointRequest::AuthorizeRelease` intentionally
refuses `EnsureStopped`. The next implementation must consume a successful
outbox marker, validate `FixtureStopReleaseProposalV1` against independently
recomputed digests, then perform explicit supervisor release/submit ordering.
It must persist an accepted `qmstop` receipt or an `Ambiguous` transport outcome;
neither result is a stopped-power claim. No release/send path is currently
connected, and production remains read-only.

The follow-on fixture IPC contract `FixtureStopReleaseProposalV1` binds the
operation, attempt, lease owner, generation, request/receipt/sample digests,
and sealed provenance for a future supervisor release/send step. It requires
canonical lowercase 64-character SHA-256 digests and rejects malformed or
operation-mismatched inputs. It is non-authorizing and validated independently;
no physical stop or stopped-power claim follows from constructing it. Checkpoint
transport outcomes are classified as accepted, refused, or ambiguous;
timeout/unavailable remains ambiguous and cannot authorize resend. The proposal
also exposes a pure evidence-digest join that must match independently
recomputed request, receipt, and sample digests; tampering is rejected without
any release or send side effect.

At `d236dedb`, trusted origins persist immutable `credential_sink_id` policy:
NULL retains physical-only fixture behavior; a stable non-nil sink requires
credential delivery. Replay must match that policy. Commits `9357287b` and
`7fea7ed2` now provide atomic session/alias/dispatch preparation, a private
fixture sink, closed acknowledgement, one-time exposure, and explicit
operation-controller routing. Commit `d236dedb` adds a dedicated
unacknowledged-delivery reclaim after real lease expiry, preserving the
original dispatch and fencing the old worker; exposed expiry remains
conservatively `Unknown`. The focused proofs cover concurrent registration,
two-scheduler refusal, immutable policy, exact-byte delivery replay,
acknowledgement/exposure ordering, controller single-send behavior, replacement
claim races, cancellation, and exposed-expiry refusal. See
`fixture-origin-delivery-policy.md` and `fixture-credential-delivery-store.md`.

Production-candidate acceptance remains open. The private fixture sink performs
synchronous filesystem writes/fsync inside the owned future and needs bounded
ownership before production use. Broader process-death qualification across
all required stages, accepting/authenticated callback result handling beyond
the PeRegister lease, production legacy-run import, later stages, service wiring, operator handoff/rollback,
and current-source Linux runtime qualification remain open. Commit `20e54b54`
adds a fixture-only PeRegister lease that inherits the original StartPe
registration scope and deadline, validates delivered/exposed provenance on
reload, and proves replacement/cancellation/deadline behavior. Callback result
and action exposure remain closed. The callback
boundary assessment in `callback-after-delivery-assessment.md` confirms that
the existing refusal-only callback/probe cannot be presented as authenticated
PeRegister compatibility; the required separate callback fence, identity
binding, replay, rollback and process-loss tests remain an explicit gate.
Accepting callback action/result surfaces and production legacy-run import
remain unimplemented. The controller now exposes the fixture PeRegister
transaction through its explicit delivery configuration; this remains
fixture-only and is not full legacy callback compatibility. GitHub checks
observed for `72a3ec61` on 2026-09-11 had Linux in
progress and macOS queued; this is not completed CI evidence. No exact-source
Linux runtime qualification is recorded for this current source. Historical
sections below retain the limits at their named revisions.

Commit `3bfcd11c` binds the fixture delivery path to an immutable selected
completion-package schema, preserving historical origins while making the
canonical `boot-files-staged.v1` definition available to arming, issuance and
reload. Commit `2d82f114` adds fixture-only authenticated PeComplete report
adjudication and an atomic, lease-free parked shutdown-grace record. Equivalent
reports replay immutably; conflicting, invalid, stale, cancelled, or
unsupported reports fail closed. The grace due projection can be repaired from
immutable history. Commit `6770f677` adds the fixture-only elapsed-grace
sweeper proof: after the immutable due time it selects `Unknown` with
`ShutdownGraceDeadlineExpired`, preserves the completion anchor, and creates no
lease. Pre-due cancellation and concurrent/reload/rollback paths are covered.
This still does not prove the VM stopped or authorize EnsureStopped. Commit
`ed68e042` adds the fixture store/native-fake EnsureStopped proof after guarded
grace expiry: physical history is reconstructed, a fresh stop mutation scope is
opened, dispatch/receipt/outcome/reload and duplicate refusal are verified, and
only the explicitly credential-enabled scheduler discovers the due stop stage.
Commit `4f8bd980` adds the durable fixture-IPC typed stop envelope, exact
StartPe physical-predecessor binding, qmstop-only receipt validation, and
restart/refusal proofs with zero attempts/effects. Supervisor release and
generic mutation submission remain closed, so the external fixture ledger still
lacks independently recoverable stop acceptance. Commit `eb917e71` adds the
independent-process refusal proof: a killed/reaped stop worker, including after
daemon restart, cannot release or submit and leaves zero attempts/effects;
stale owner/generation is rejected. This is refusal evidence only. Durable stop
admission, stop receipt/power publication, restoration/fencing, and controller
dispatch/recovery after an accepted stop remain open. Commit `4adb3abd` adds a
supervisor-only immutable stop-admission frame and focused torn/checksum/replay,
generation, power, and worker-death refusal proofs. It asserts scheduler
authority but does not independently query PostgreSQL, and it deliberately does
not release stop dispatch. A refreshable post-grace current-power publication is
still required. Commit `23fa9553` adds a separate durable post-grace
Running-power refresh bound to the accepted StartPe effect and rejects stale,
nonmonotonic, or acceptance-clock-rewritten observations while preserving
legacy admission bytes. It proves refresh/replay without attempts, effects, or
Stopped publication. Commit `a8079c03` adds scheduler-owned stop authority
derived from locked PostgreSQL guarded-grace/current-lease history, including
competing-worker refusal. The supervisor current-power IPC consumer and
connected release/physical-stop path remain open. Commit `c4e152f4` adds a
supervisor-only typed Running-power publication boundary tied to exact StartPe
identity/request/receipt, daemon generation, and explicit sample timestamp and
status. It rejects worker-origin, stale/future/duplicate, and restarted-
generation samples while preserving immutable completion bytes. The fixture
world still lacks a live power-read source, so the scheduler-authority to
publication to AdmitStop consumer remains open; no stop release, dispatch,
reconciliation, or Stopped claim follows from this boundary. Commit `ecb9138c`
adds an explicit supervisor `consume_stop_current_power` refusal when no live
power reader exists; valid StartPe identity is accepted for diagnosis, but no
publication, PostgreSQL replacement, admission, release, attempts, or effects
occur. This makes the missing runtime source executable evidence rather than a
success claim. The generic callback
mapping table below remains intentionally pending for legacy action/result
surfaces. This fixture-only slice is not full callback compatibility or the
entire port.

Commit `029555f1` adds safe operation-scoped fixture
port resolution. An immutable per-operation binding is retained across the
complete invocation, and the capacity-two interleaving proof verifies isolated
effects, receipts, PostgreSQL terminal projections, duplicate-binding
rejection, and missing-binding refusal. This isolation proof does not establish
later-stage completion; current-source Linux qualification is still required.

The authenticated session contract remains incomplete.
`postgres-store/src/start_pe_session.rs` accepts only the
`AuthenticatedPeWitnessV1::Unavailable` variant, and its opt-in proposal schema
is explicitly diagnostic/read-only. The later decision and credential-session
integration artifacts are now tracked. The fixture boot session in `518878d7`
does not implement an authenticated witness or credential ownership; those
must be integrated before authenticated callback progression can be claimed.

Commit `aadf5c83` adds the first compatibility implementation for the legacy
run bearer: canonical Python HMAC-SHA256 verification, closed claims, exact
expiry behavior, wrong-run/tamper refusal, and compile-fail construction
proofs. It deliberately does not turn the bearer into session or StartPe
authority. Fixture preparation/arming subsequently landed in `518878d7`;
authenticated session integration remains a subsequent gate.

Commit `aa9755f0` adds the first trusted preparation input: a closed
`RegisteredPePackageSemanticsV1` derived exclusively from the store-validated
registration. Its versioned digest binds the run, StartPe operation, and full
admitted plan fingerprint; caller-supplied replacement IDs, roles, artifacts,
URLs, credentials, and digests are not accepted. This is semantic provenance,
not delivered-package attestation, session issuance, callback authentication,
or dispatch authority.

Commit `a65afa93` adds a closed `MaterializedPePackageSemanticsV1` envelope
with canonical versioned semantic bytes and a separate digest. It is derived
only from the validated registration, redacts plan/bytes from `Debug`, and
contains no delivery credentials, URLs, secrets, issuance identity, session
authority, or StartPe admission. This is the concrete input for future private
preparation persistence, not proof of delivery or callback authentication.

Commit `dc0a6871` adds a trusted-input canonical legacy bearer issuer. It
preserves Python’s compact sorted JSON and HMAC-SHA256 wire behavior for string
and integer run identities, rejects unsafe inputs, keeps issued credentials
private/redacted, and requires explicit delivery exposure. Deterministic
reissue is tested; the issuer itself grants no session, callback, or StartPe
authority and does not change the original registration deadline.

Commit `b9c47ac5` pins the credential/session transaction integration rather
than adding an unsafe process-local registry. The durable design requires
unique credential ownership, immutable run/operation/attempt binding, same-
session renewal, cross-session conflict, restart retention, and atomic
alias/session/dispatch insertion under current scheduler locks. This remains
a credential-association design artifact. The subsequent fixture session
migration and boot arming do not implement credential aliases or callbacks.

Historical checkpoint: commit `08471af3` records that the exact-source Linux
reaping diagnostic image built successfully, but its retained targeted run
ended without output before producing PID/reap evidence. The attempt is
terminal and observationally inconclusive; it adds no Linux pass claim and the
strict reaping gate remains open.

MacOS verification checkpoint at `f83185f6`: `cargo fmt --all -- --check`,
workspace all-features Clippy with `-D warnings`, the 24-case operation decision
suite, and the controller-service suite (58 unit plus 45 service tests) pass.
The full all-features workspace run remains red in
`configure_worker_death_after_publication_preserves_prefix_and_uncertainty`:
the controller checkpoint timed out and the worker reported `Storage`. This is
under focused reproduction and is not yet classified as either a Rust defect
or retained Docker/fixture startup failure.

Focused follow-up at `75e3a8f3` reran
`configure_worker_death_after_publication_preserves_prefix_and_uncertainty`
twice, serially with `RUST_MIN_STACK=16777216`, and both runs passed. Each run
created the owned PostgreSQL fixture, completed the initial and recovery worker,
and recovered the final ledger without timeout or `Storage` failure. This is
positive focused recovery evidence, but the earlier broad all-features workspace
run remains unresolved because suite-level fixture contention was not ruled out.

Clean serial all-features workspace verification at `8c1cbb69` then completed
successfully with `RUST_MIN_STACK=16777216`, offline/locked Cargo, and one test
thread. It passed the full workspace test and doctest set, including the
previously red worker-death path, PostgreSQL-native (60), PostgreSQL OSDeploy
(65), durability (128), registration (51), scheduler (62), fixture, visibility,
compatibility, and service suites, with zero failures. This closes the broad
macOS test gate for that source; it does not close exact-source Linux or the
remaining production integration gates.

The next exact-source Linux attempt targeted `a5ef79e9bd712df563c593284a408d2324649ad6`.
Release workspace/examples and `controller-service` compilation completed inside
the approved `linux/amd64` Docker workflow, but Docker/buildx stopped producing
output during workspace test precompilation and remained unresponsive for more
than seven minutes. Only the local build-client processes were terminated;
OrbStack was not restarted and no retained containers/images were removed. No
image publication, executable seal, Linux test receipt, or qualification result
can be claimed from this attempt.

Current exact-source Linux build at `becaf0e2` completed successfully as image
`sha256:7c766f2c552751bf988e20cf94a33e3309a17f41f2dbd26c7531cf63a02fd560`.
The sealed `linux/amd64` image passed its Dockerfile release/test-precompile
gates and a network-disabled direct `pve-port --features fixture-ipc` run (36
tests, one ordinary doctest, and 22 compile-fail doctests). The owned-v1
PostgreSQL launcher remains sealed to an older image/source and therefore was
not retargeted; current-source owned-v1 qualification is still open.

The launcher was subsequently retargeted through the runtime-source seal to the
current image built from `f92a6287` (`sha256:5f5ddae5...`). Smoke mode passed
in `requalification-f92a6287-smoke-2`. Bounded full mode in
`requalification-f92a6287-full-2` failed within its bound: the runner exited
101, four targets failed, compile-fail doctest/API mismatches were reported,
some PostgreSQL tests lacked the explicitly required isolated database, and
native fake-child cleanup left an unconfirmed reap. The launcher retained the
containers and reported `ValueError: child group remains`; the qualification
field is consequently `INCOMPLETE`. This is retained as a failed Linux
qualification result and is not evidence of production readiness.

The corrected full rerun used launcher commit `a0e36390` against the same
exact-source image. The runner reached terminal exit `1` without OOM or restart.
The DSN-only tests were excluded as intended and the repeated native cleanup
and reap helper tests passed, but the operation-controller recovery group still
failed with fixture-worker `Storage`, recovery `Elapsed`, and initial-worker
`BrokenPipe` outcomes. The complete recovered output is retained at
`requalification-a0e36390-full-1/runner-docker.log`; the host launcher had been
detached before it could write its final state. This narrows the remaining Linux
blocker to controller-fixture recovery under the full workload; it does not
establish Linux qualification or production readiness.

Astra's diagnosis does not currently support a production Rust defect: the two
failed parent tests are supervised fixture-worker recovery cases, and the
retained parent log lacks the worker's direct stderr needed to distinguish
`BrokenPipe`, `Storage`, and `Elapsed` as controller defects. Both recovery tests
pass in the focused macOS lane with `RUST_MIN_STACK=16777216` (already set by
the Linux launcher), while the storage, budget, and process helper tests pass in
the Linux run. A targeted sealed Linux execution with direct worker output is
required before classifying the remaining failure or changing source.

Commit `c9e1ca5a` adds a bounded recovery-only Linux lane for the two
operation-controller worker-recovery tests. Its first sealed attempt was
refused before container creation because Docker image inspection exceeded the
three-second child deadline (`ValueError: child deadline`). This attempt is
therefore an infrastructure/API-responsiveness blocker, not a recovery result;
it must be retried with a fresh evidence directory after Docker responds within
the existing bound.

A targeted macOS retry of those two recovery tests, with the required
`RUST_MIN_STACK=16777216`, also stopped before controller execution because the
owned PostgreSQL fixture returned `local_database_unavailable: local_process_timeout`.
The same session's read-only Docker API probe exceeded four seconds. This is
retained as fixture admission failure and does not justify a Rust source change.

After Docker responsiveness returned, the bounded recovery-only Linux lane was
rerun successfully. The sealed evidence in
`requalification-c9e1ca5a-recovery-3/` shows both
`configure_worker_death_after_*` tests passing (2/2 in 84.01 seconds), with
owned PostgreSQL, cgroup2 limits, and zero OOM events. This closes the targeted
Linux recovery gate for the sealed source, but not the broader full Linux
qualification or production-readiness gates.

## Decision

**Not production-ready and not approved for cutover.** The Rust controller is a strong local proof-of-concept candidate, but the evidence does not yet establish a safe replacement for the production controller or Ansible execution path.

## Proven locally

- Commit `857b485` verifies stage-aware Clone and DiskCapacity/resize post-dispatch publication/readback with exact durable-effect identity, owner/generation/attempt/digest binding, restart invalidation, duplicate refusal, and seven focused tests. The `FixtureProvisioningPort` adapter and full three-stage controller progression are still incomplete.
- Commit `2450784` wires the adapter's exact DiskCapacity/resize request through stage checkpointing, durable Clone predecessor validation, stage submission, and publication/readback, while preserving legacy Clone behavior. The controller-level request generation/journaling and ConfigurePe publication contract remain open.
- Commit `6d7be96` preserves legacy Clone provenance while explicitly authorizing a subsequent v2 resize only from the byte-identical durable Clone effect, exact receipt, world capacity, and current supervisor authority. This is a bridge contract, not yet an IPC end-to-end or fresh controller Clone→resize proof.
- Commit `657ecb1` verifies that bridge over three daemon lifetimes, including byte-identical receipt recovery, exact one-time resize authorization, and fail-closed fabricated/reassigned/duplicate cases. The generated controller resize request and original v1 predecessor readback are still not wired into a fresh controller chain.
- Commit `d30c14c` supplies the late-binding adapter API for a controller-generated resize after accepted Clone, including checkpoint-time exact request binding, one-time release-gated submission, and observation-only receipt restoration. A fresh PostgreSQL controller Clone→DiskCapacity proof still must orchestrate supervisor authorization/publication; ConfigurePe remains unintegrated.
- Commit `49b6782` verifies that fresh PostgreSQL controller Clone→DiskCapacity orchestration end to end: real `run_osdeploy_once` requests, durable dispatch observed independently, exact legacy predecessor bridge, daemon resize receipt, journaled publication, and final `Decided(Satisfied)`. This materially advances the local PoC but does not establish ConfigurePe, remaining stages, current-source Linux, service/callback compatibility, or production readiness.
- Commit `40c29be` verifies a synchronous ConfigurePe publication/readback contract over the fixture daemon, deliberately without task/UPID semantics. The adapter and controller still need late binding, accepted-resize predecessor collection, and a fresh PostgreSQL end-to-end satisfaction proof.
- Commit `87afa83` verifies the fresh PostgreSQL Clone → DiskCapacity → ConfigurePe controller prefix end to end, including exact journaled requests/receipts, three attempts/effects, synchronous PE acceptance/readback, and replay rejection. It is still only a prefix: later stages, process-loss recovery, compatibility, single-writer handoff, Linux requalification, and production gates remain unproven.
- Commit `6a52130` adds an explicit feature-enabled Linux fixture qualification workload; it has only launcher-test evidence so far, not a current-source Linux runtime result.
- The previously retained Linux qualification was stale: its owned-v1 image was sealed to `ac03e96c...`, while the Rust source had advanced by 47 inputs. That gap has now been addressed for the fixture runtime by a fresh approved OrbStack build and explicit feature-gated `pve-port` execution; broader service/recovery qualification remains separate.
- Current-source fixture Linux runtime is now verified in `restart-task9-owned-full-fixture-current-3`: image `sha256:d7e20369...`, source `8a98d7f...`, 33 successful result groups including `pve-port --features fixture-ipc`, and zero cgroup OOM/event counters under the admitted profile. The owned harness intentionally remains `qualification: INCOMPLETE`; broader Linux service/recovery, compatibility, and production gates are still open.
- Commit `f107122` adds separate-process acceptance and ConfigurePe death/recovery proofs over the three-stage prefix. Both kill windows preserve the original attempt/dispatch and fail closed as `Unknown` without a durable receipt; daemon restart recovers exactly three effects. This is meaningful process-recovery evidence, but not full service recovery or automatic completion after receipt persistence. The test's required larger stack and retained disposable fixture are recorded in `prefix-worker-recovery-acceptance.md`.
- Commit `b166006` adds the persisted-receipt recovery case: after an actual worker dies with a durable exact receipt, a new worker waits for lease expiry and reconciles the prefix to `Satisfied` using fresh observations, with no duplicate claim/effect and exactly three attempts/effects. This strengthens recovery assurance but does not cover all later stages, full service/callback compatibility, or current-source Linux execution of the newest recovery source.
- Commit `570c6fc` establishes a typed StartPe contract and explicit fail-closed durable-power-state gate; it does not claim StartPe execution. Commit `bd7f05c` supplies a 17-case callback compatibility corpus and identifies missing Rust callback/result atomicity decisions. These are preparation gates, not production compatibility or full workflow acceptance.
- Commit `885f39a` verifies versioned durable power-observation replay and an explicit stopped ConfigurePe baseline over IPC. StartPe execution remains intentionally disabled until the running transition, atomic admission, and task/publication semantics are proven.
- Commit `17f365e` verifies through the real daemon that a durable stopped baseline remains fail-closed for StartPe across restart and repeated submission, with authorization released and no ledger mutation. This closes a safety regression gate but leaves atomic running-transition admission, independent task/power observation, adapter recovery binding, and full PostgreSQL/process-death coverage open.
- Commit `6cfa7d7` supplies an executable checksummed/fsynced atomic StartPe ledger seam with replay and corruption protections. It is a private fixture seam only; typed power-aware IPC, adapter/controller dispatch, PostgreSQL recovery, and production integration are not yet proven.
- Commit `cff34b3` supplies pure callback result/phase classifiers that retain failure and replay/conflict distinctions without adding unsupported HTTP routes or PeRegister success. It improves compatibility evidence but does not establish authenticated session binding or callback production readiness.
- Commit `a908521` supplies typed fixture IPC authorization bound to exact durable stopped power evidence, with stale, forged, duplicate, and restart-replay refusal. This is authorization evidence only; it does not establish StartPe execution, controller satisfaction, adapter integration, or PostgreSQL recovery.
- Commit `cc9c6e1` adds submission-time revalidation and atomic synthetic StartPe admission through fixture IPC. This remains distinct from real `qmstart`, controller satisfaction, independent task/running observation, adapter restoration, and PostgreSQL/process-death proof.
- Commit `6b95810` adds durable typed task/running publication bound to the atomic StartPe record, with replay and contradiction/torn-record protections. These are trusted fixture-supervisor observations only and do not establish live Proxmox evidence, adapter restoration, PostgreSQL satisfaction, or production readiness.
- Commit `5e2798a` adds read-only typed restoration with exact identity, receipt, task, power, and timestamp checks. It does not wire the general provisioning adapter, establish full postconditions, prove PostgreSQL satisfaction, or authorize production mutation.
- Commit `618d3eac` records an executable identity-compatibility gate: opaque PVE configuration digests cannot be relabeled as legacy SHA-256 inventory identities. A versioned additive inventory contract is required; no adapter satisfaction or production readiness is claimed.
- Commit `faa5c01b` adds that additive stage-bound inventory v2 contract with strict identity, coverage, timestamp, uniqueness, and malformed/relabeling refusal checks. It grants no publication or dispatch authority and does not establish full postconditions, controller satisfaction, or production readiness.
- Commit `b2660e0e` adds supervisor-only full StartPe publication with v2 inventory, media/infrastructure, and durable task/power evidence, including exclusive fsynced persistence and replay refusal. It remains fixture evidence only and does not establish adapter restoration, PostgreSQL progression/recovery, guest stages, or production readiness.
- Commit `19ea3c17` adds typed full-bundle readback with replay and forged-checksum refusal. This strengthens evidence restoration only; it does not wire general adapter satisfaction or prove PostgreSQL progression, process-death recovery, or production readiness.
- Commit `f4b7179` adds a PostgreSQL negative admission proof: StartPe evidence is rejected before the action/scheduler/lifecycle gates, with no database mutation or dispatch. This is a safety gate, not positive four-stage acceptance or production readiness.
- Commit `096ba407` adds a read-only adapter validation outcome for complete full StartPe evidence, with missing-evidence preservation and pre-I/O identity/context refusal. It does not integrate the PostgreSQL caller, prove worker-death recovery, or establish production readiness.
- Commit `f2cc6d38` adds separate-process full-publication worker/daemon recovery with byte-identical replay and duplicate refusal. It does not establish PostgreSQL receipt durability, controller satisfaction, later guest stages, or production readiness.
- Commit `58a3c3e` adds a default-disabled read-only PostgreSQL diagnostic ingress that cannot create attempts, dispatch, decisions, sessions, or satisfaction. Runtime DB-unchanged assertions are not claimed because owned PostgreSQL startup timed out; this is not production readiness.
- Commit `09fc0058` defines typed fail-closed session arming with immutable deadline/context binding and unavailable/expired refusal. It is not an atomic PostgreSQL arming transaction, authenticated session verification, crash proof, or production readiness claim.
- Commit `b18b8993` adds an opt-in capability-closed PostgreSQL session seam with pre-connection unavailable refusal and rolled-back diagnostic context comparison. The SQL runtime check was not executed; authenticated session creation, atomic persistence, fence mapping, and production readiness remain unproven.
- Commit `f415b70a` adds exact microsecond PE-registration anchor representation and rejects lossy v1 downgrade. It is not yet integrated into PostgreSQL session persistence and does not establish dispatch, satisfaction, or production readiness.
- Commit `0f9e1180` integrates exact v2 anchor comparison into the opt-in diagnostic probe while retaining witness-unavailable/expired refusal. The durable-row/schema runtime test is still ignored; no session, dispatch, satisfaction, or production readiness is claimed.
- Commit `d5602ab4` defines typed fail-closed PeRegister witness binding and refusal semantics. It does not provide an authenticated verifier, HTTP policy, persistence, dispatch, crash proof, or production readiness.
- Commit `47d13f24` adds pure authority/fence descriptor checks with refusal-only outcomes. It does not provide authenticated witness handling, store-derived fencing, persistence, API routes, dispatch, crash proof, or production readiness.
- Commit `c204c6c0` adds a refusal-only result transaction contract with Docker-free fence/conflict/expiry tests. It does not execute SQL rollback/CAS, authenticate replay, recover commit-to-response crashes, dispatch, or establish production readiness.
- Commit `4ed4a23b` adds a fixture-only rollback/fence probe with no persisted result or acceptance. The SQL runtime test was not executed without an explicitly supplied schema-test DSN, so live rollback/concurrency, authenticated authority, and crash recovery remain unproven.
- Commit `7a486f4` proves the rollback/concurrency behavior against an owned loopback PostgreSQL container, including advisory-lock conflict/release, refusal-only disposition, no schema creation, and zero residual locks. It does not prove authenticated result CAS, session authority, or crash recovery.
- Commit `e6d3894` adds fail-closed PeComplete scope and report validation with exact steps and deadline anchoring. It does not authenticate PeRegister/PeComplete results, integrate the scheduler, prove later stages, or establish production readiness.
- Commit `044e66e` adds fail-closed PeShutdownGrace/PeEnsureStopped scope and deadline validation. It does not authorize force-stop, authenticate completion, prove fresh VM power evidence, persist results, or establish later-stage/production readiness.
- Commit `08c9af0` adds fail-closed ConfigureDisk/StartDisk evidence binding and refusal tests. Descriptive volume/configuration strings do not prove ownership, attachment, capacity, trusted receipts, execution fences, or production readiness.
- Commit `a37ba82` adds fail-closed InstallQga host-QGA evidence checks. It cannot authenticate an agent or authorize installation and does not establish VerifyQga, watchdog, package identity, durable receipts, or production readiness.
- Commit `7f9a2ab` adds fail-closed VerifyQga receipt identity binding. It does not verify the receipt, authenticate host transport, establish freshness/execution fences, persist results, or establish production readiness.
- Commit `a187aed` adds fail-closed InstallQgaWatchdog receipt/agent binding. It does not authenticate the agent, verify trusted history, prove package/service postconditions, install, dispatch, or establish production readiness.
- Commit `ed76e78` adds fail-closed InstallAgent artifact identity binding. It does not verify artifact bytes/signatures, enroll the agent, prove service state, install, dispatch, or establish production readiness.
- Commit `c9aa2f6` adds fail-closed AgentHeartbeat identity/freshness/sequence binding. It does not authenticate the agent, verify trusted timestamps, persist sequence CAS, accept heartbeats, dispatch, or establish production readiness.
- Commit `8ef7f77` adds fail-closed VerifyOperational aggregate validation. Its booleans remain descriptive claims, not independently verified service/package/postcondition evidence; authenticated observations, durable results, fences, integration, and production readiness remain open.
- Commit `5b59621` adds fail-closed export hash and rollback-window integrity checks. It does not prove signatures, manifest provenance, restore safety, database rollback compatibility, deployment, or production readiness.

- The changed-source Linux qualification evidence covers Rust source `ac03e96caa70fadd9a572f5c206d03e1ec1e0121`, with launcher binding `e30454e72b132055dcf8aba926a182957ac19f99`; later commits contain only evidence/documentation updates.
- Commit `7f556bfd` records a newer exact-source Linux/amd64 image for `6f582095` with all Dockerfile runtime gates and 254 verified source blobs. The owned fixture run did not start because image inspection timed out and Docker later stalled; no fixture runtime qualification is claimed.
- Commit `6894534` records the successful frozen-source Linux fixture retry with 34 successful result groups, exact image/source identity, zero restart/OOM events, and retained containers/receipts. It is feature-fixture evidence only; the launcher remains `INCOMPLETE` and does not prove full controller qualification or production readiness.
- Commit `000c3d81` records fresh exact-source Linux fixture evidence for `93e04827` with 264 blob verification, 34 successful groups, zero memory events, and no restart/OOM kills. It does not prove full PostgreSQL/controller progression, later stages, or production readiness.
- Commit `4dd12e0` improves child-reaping diagnostics after the `c3e38135` Linux gate failed on a zombie/absence assertion. All 14 macOS process tests pass, but the Linux gate is still red and no newer Linux qualification or production readiness is claimed.
- Commit `0bffdf2` documents why ownership handoff cannot prove bounded absence when a child remains pending at the deadline. This is an unresolved fixture assurance gate; no Linux qualification or production readiness is claimed.
- Commit `d5209aa` records that owner join/drop reordering would weaken autonomous supervision evidence and cannot repair an already abandoned child. The macOS test passes, but the Linux child-reaping gate remains unresolved.
- The final macOS four-package regression at `557bd12` passed 665 tests, with 3 intentionally ignored, under a bounded supervisor.
- The prior full Linux/amd64 qualification image was built from `49b0877` and inspected as `sha256:d2a7622623953ba9342e11ed1d6df00d8dc62c1d62713bdf94e70f9135cb459c`. After the guest-action change, a fresh exact-source image was built as `sha256:6c2c32025ce3a2d610c5f3be18d27b1f78a60e6b2e60257023ba8c36503fcdda`; the owned-v1 smoke and changed-source full Linux gates both passed. Full evidence is retained in `restart-task9-owned-full-guest-1/`.
- The changed-source full run completed with exit 0 under the bounded supervisor. Its 23 test-result groups reported zero failures, with zero cgroup memory OOM/event counters and exact source/image/launcher bindings.
- The isolated Linux Compose proof passed the three-worker synthetic scheduling, PostgreSQL, cancellation/recovery, native adapter, and descendant-cleanup scenarios (12 lifecycle plus 4 native PostgreSQL tests).
- Python compatibility-side contracts passed with explicit Python 3.12: producer 2/2, proof-wait 5/5, and proof-coordination 1/1.
- Rust formatting, strict offline/locked Clippy, focused protocol checks, and bounded child/process cleanup evidence passed for the accepted local source changes.
- Native real-PostgreSQL restart/recovery cases passed for restart fencing, unknown-state reconciliation, receipt reload, aged-infrastructure continuation, and response-loss handling; the combined record is `restart-recovery-1/acceptance.md`.
- The guest-action identity slice is locally verified, but authoritative callback/session decisions are missing; no authenticated callback exposure or result-ingest claim is made.
- A test-only durable fixture attempt ledger is verified at `961ef7283cf46ba2789c3ac2b68e8df48e99fe70` (four focused tests plus targeted strict Clippy). It preserves duplicate attempts and fails closed on corrupt/partial records, but is not yet an IPC daemon or accepted-world/effect store.
- Accepted synthetic VM world transitions are now durably bound and replayed by the same test-only ledger at `0c157d86caa8074994f11e3837128da3cb9d30dc` (six focused tests, formatting, and targeted strict Clippy). This remains fixture infrastructure only; it does not prove IPC, task/UPID persistence, or independent worker recovery.
- A bounded test-only Unix-socket fixture daemon is verified at `8e7409633c48e9b5255ecd07d2c6b2aaecfe643b` (six ledger tests, two subprocess protocol tests, formatting, and targeted strict Clippy). It proves client/supervisor control separation and bounded request handling, but not peer authentication, daemon restart/rebind, controller wiring, task/UPID state, or independent worker recovery.
- Stale-endpoint and daemon-rebind behavior is verified at `a1bfd719e3462bf409dda73555515faf194ae8fe` (four daemon tests, formatting, and targeted strict Clippy). Direct restart refuses stale endpoints; supervisor-owned cleanup is required before rebind; corrupt ledgers and endpoint substitution fail closed. This still does not prove controller wiring, task/UPID state, or worker A/B recovery.
- Accepted-effect and per-VM world status are now available only through the test daemon protocol at `d0fed1cf530007f35e65432fdd70cd382c615dbd` (six ledger tests, five daemon tests, formatting, and targeted strict Clippy). Effects commit before replies and recover across daemon restart; this still does not prove controller wiring, task/UPID state, or independent worker recovery.
- Controller integration remains a distinct open seam: the default `OsDeployController` path remains concretely tied to `NativeFakePve`, while the opt-in fixture capability seam is now recorded at `f78bb5f`; the fixture protocol still accepts caller-supplied synthetic state and has no typed end-to-end mutation path. The next proof must establish daemon-owned facts and one typed Clone round-trip before any A/B worker-death claim.
- A default-disabled `fixture-ipc` feature now exports reusable test daemon/ledger support (`25e4f56`), and typed Clone request/receipt envelopes are verified at `9ecd6c5`; a macOS Unix-socket timeout portability correction is `4a29b46`. These remain message/fixture contracts only; the typed end-to-end Clone path is still open.
- The sealed `fixture-ipc::ControllerFixturePort` checkpoint seam is verified at `3f2f4ee` (trait-object barrier runtime test, observer compile-fail contract, 19 feature docs, default check, formatting, and strict Clippy). It preserves the existing NativeFakePve checkpoint but does not yet adapt dispatch permits or provide typed IPC provisioning.
- The consuming dispatch permit seam is committed at `e34eacc`, with Arc caller compatibility fixed in `e8bdd27` and `feaf463`. Postgres-store all-target compilation, 22 doctests, and strict Clippy pass. The runtime database suite remains to be rerun; fixture IPC submission and daemon-backed provisioning facts are still open.
- The opt-in controller ownership/helper adaptation is now committed at `f78bb5f`: `operation-controller/fixture-ipc` stores the sealed `ControllerFixturePort` and exposes `new_fixture`, while the default NativeFakePve constructor is preserved. Feature/default checks, feature library tests and doctests, formatting, and strict Clippy pass. This does not establish daemon-backed provisioning capabilities, a controller Clone round-trip, or worker recovery.
- Commit `1254a94` adds a digest-bound daemon `accepted_effect` read that returns only durably committed effects and survives daemon restart; malformed bindings and attempt-only/absent state remain fail-closed. This strengthens recovery evidence but remains fixture-log substrate, not a `ProvisioningFakePort` implementation or controller/process takeover proof.
- Commit `488cf90` adds the feature-gated bounded `FixtureReadClient` for status, world, and accepted-effect observations. It is strictly read-only, deadline-bounded across connect/write/read, and validates reply framing and identity. This is reusable transport substrate; daemon-owned preflight/task facts, mutation capability, controller Clone round-trip, and independent takeover remain unproven.
- Commit `f0abd54` pins the negative capability boundary: `FixtureReadClient` cannot satisfy any provisioning, controller-fixture, or preflight trait, and the seam plan maps the missing daemon-owned facts required for a truthful implementation. No placeholder adapter was added; the typed controller Clone and process-recovery gates remain open.
- Commit `4c7f6c7` adds a bounded, supervisor-seeded inventory snapshot prefix with explicit unavailable state, strict validation, restart stability, and immutable startup observations. Its limitations are explicit in `fixture-snapshot-protocol.md`: it is historical inventory, not current Proxmox state, and lacks storage/network/task/UPID/full identity/configuration facts. It does not implement provisioning capability or advance controller acceptance.
- Commit `2f1cfe8` adds a typed, freshness-checked projection of the historical inventory while preserving fixture/node identity and source timestamp. It deliberately rejects mismatches and leaves absent power/configuration/task facts unavailable, so it does not advance preflight, dispatch, controller Clone, or worker-recovery readiness.
- Commit `aa2d278` adds a strictly read-only, identity-bound task/UPID observation prefix with explicit lifecycle states and restart-stable seeded reads. It is loaded once per daemon lifetime and does not execute or persist task transitions; the production UPID parser, mutation capability, controller Clone round-trip, and process takeover gates remain unproven.
- Commit `76d5e01` adds a separate feature-gated `FixtureMutationClient` for one supervisor-seeded, exact-request Clone. Attempt-before-check, daemon-generated UPID, and sync-before-reply receipt/effect persistence are verified across restart. This proves a bounded synthetic socket mutation, not full provisioning discovery, task progression, controller integration, or independent worker recovery.
- Commit `1a91843` executes the real Clone preflight evaluator with each required observation family removed in turn, proving incomplete daemon reads close admission and that absent target power is not invented when target absence is independently established. The companion mapping defines the remaining daemon seed, replay, checkpoint, and PostgreSQL/controller acceptance steps. This is a prerequisite gate, not controller integration or production readiness.
- Commit `0108405` adds a strict supervisor `FixtureCloneReads` seed schema for node, storage, bridges, and complete inventory/identity facts, preserving timestamps and explicit errors. It is validated and bounded but not yet loaded by the daemon or projected into provisioning capability; full config/media/task facts and controller acceptance remain open.
- Commit `716c149` exposes those reads through daemon startup and `FixtureReadClient::clone_reads`, with identity-bound requests, strict response caps, restart stability, and no ledger writes. Serial feature/default suites, Clippy, and formatting pass; a parallel run reproduced an existing `NotConnected` test flake, so concurrency qualification remains open. This does not implement the provisioning port or controller Clone round-trip.
- Commit `84c7213` adds a strict identity-bound seed/transport for source and target provisioning configs, separate power reads, and deployment/driver media. It preserves timestamps and explicit errors and does not infer absence or grant capability. Its populated-schema and restart/client proofs are not yet complete, so it is not a qualified provisioning adapter or controller integration.
- Commit `c296ce1` completes those populated schema and restart/client proofs, including all six identity bindings, timestamp/error checks, corrupt-startup failure, two restarts, and zero mutation attempts. Target configuration and power remain explicit errors, so this is still read-only fixture evidence rather than a `ProvisioningFakePort`, controller Clone round-trip, or recovery qualification.
- Commit `05fbc27` proves seeded present-target configuration/running power and explicit target-absence semantics across restarts, with strict identity/timestamp/power validation and no ledger writes. This removes ambiguity in the read seed but does not implement the provisioning trait, controller Clone round-trip, task execution, or worker recovery.
- Commit `2697867` pins two further admission prerequisites: an explicit unlocked (`locked == Some(false)`) source power fact and per-identity complete coverage. The current seed omits both, so projecting them would fabricate authority; missing-fact regressions correctly prevent `Ready`. No provisioning adapter or controller round-trip is claimed.
- Commit `7134591` adds those facts to the strict seed (`SeedPower.locked` and timestamped source/target coverage), with invalid/missing values rejected and partial coverage preserved as partial. Restart and startup immutability proofs include the new fields; no provisioning capability or controller integration is granted yet.
- Commit `ea9c5cf` adds an executable inventory projection regression proving identity-only entries cannot become `ClusterVmInventory`: every member needs its own observed status at the inventory timestamp, including unrelated VMs. Source/target power observations cannot substitute for that evidence, so the sealed adapter remains unimplemented.
- Commit `ab373ba` adds per-entry timestamp-bound power/status observations and proves unrelated VM status survives daemon startup immutability and two restarts without ledger writes. Strict validation and projection tests pass. The inventory seed is now sufficient for this specific status fact, but the provisioning adapter, checkpoint transport, controller round-trip, and process recovery remain open.
- Commit `bd2a39c` proves the remaining identity-coverage limitation: controller collection covers every inventory member, while coverage is seeded only for source/target. Unrelated members therefore cannot be promoted to complete provisioning identities; omitted coverage fails closed. The adapter and checkpoint IPC remain unimplemented.
- Commit `3e64b8f` adds per-inventory-member timestamp-bound coverage, including partial and explicit-error states, with strict validation and two-restart preservation. This removes the identity-coverage data-model gap; it does not yet provide the sealed provisioning adapter, checkpoint transport, controller round-trip, or worker recovery.
- Commit `b05747c` adds a sealed feature-gated `FixtureProvisioningPort` that composes seeded Clone reads and typed Clone mutation while returning explicit unavailability for unseeded inherited reads and rejecting non-Clone actions. It is not a `ControllerFixturePort` because supervisor checkpoint IPC is absent; populated end-to-end daemon/adaptor proof and the PostgreSQL/controller round-trip remain open.
- Commit `3e171f3` adds a durable, owned checkpoint IPC with generation/owner/operation/dispatch binding, bounded deadlines, restart invalidation, and explicit supervisor control. Its client is fallible. At that historical checkpoint, the controller seam still returned unit, so integration was gated on safe error propagation; this was checkpoint evidence only, not controller Clone or worker-recovery proof.
- Commit `3950bc4` closes that specific error-propagation gap: `ControllerFixturePort::controller_checkpoint` now returns typed `Unavailable`, `TimedOut`, or `Rejected` errors; the native fake, IPC client, and `OsDeployController::advance` all preserve failure before dispatch submission. Focused controller/checkpoint tests, strict Clippy, and formatting pass. This remains pre-PostgreSQL/controller-roundtrip evidence; full Clone recovery, independent process takeover, and service compatibility are still open.
- Commit `e7c64b7` composes the seeded `FixtureProvisioningPort` with `FixtureCheckpointClient` through a bound `ControllerFixturePort` adapter. Exact operation/generation/owner mismatches and missing configuration fail closed, while the constructor and daemon ownership tests exercise the composed trait object. Focused feature tests, doctests, strict Clippy, and formatting pass. This does not establish PostgreSQL dispatch/receipt durability, independent worker takeover, or full service compatibility.
- Commit `bef1e21` proves the next composition boundary against isolated PostgreSQL: an independent pool sees committed dispatch, the owned checkpoint blocks the daemon effect until release, the consuming permit produces one typed daemon receipt/effect, the receipt reloads exactly from a fresh store, and duplicate submission is rejected. The dedicated integration target passed 35/35 with strict Clippy and formatting. The test uses the existing native preflight/request helper and does not yet exercise `OsDeployController::run_osdeploy_once`, full controller reconstruction, independent process takeover, or complete service compatibility.
- Commit `179f4a0` adds an independent child-process recovery reader: it opens a separate PostgreSQL connection, reconstructs the durable request, reads the exact accepted IPC effect, verifies receipt equality and sequence one, rejects a mismatched digest, and creates no additional attempt. The parent/child proof, strict Clippy, and formatting pass. This establishes independent receipt/effect reconstruction only; it does not prove worker-A termination, worker-B lease acquisition, scheduler advancement, or complete service compatibility.
- Commit `fcef733` adds a supervisor-owned worker process-loss proof. Worker A performs the real claim/start/dispatch/IPC submission/journal flow; the supervisor observes matching durable PostgreSQL and fixture receipt/effect state before terminating A; worker B reconstructs the original attempt from fresh connections, verifies the exact receipt, rejects a wrong request hash, and refuses claim/resume while the original lease is live. The parent confirms a single attempt, and the process-loss target, strict Clippy, and formatting pass. This does not establish lease-expiry scheduler takeover, crash-window receipt reconciliation, or full controller outcome/service compatibility.
- Commit `6e617a2` extends process recovery through the actual 30-second lease expiry: worker B is refused before expiry, then reaps the original attempt to `Unknown`, restarts the fixture daemon from durable state, and reconciles without creating a replacement attempt. Exact receipt/effect and sequence one persist, with one PostgreSQL attempt. The final-source proof, strict Clippy, formatting, and diff checks pass. This remains supervisor-assisted and does not prove successful task/target reconciliation, the effect-before-journal crash window, or the complete service loop.
- Commit `156a796` proves fail-closed handling when the fixture effect is accepted before its PostgreSQL receipt is journaled. A deterministic test-only pause lets the supervisor verify effect-present/receipt-absent state before killing worker A; worker B naturally waits for lease expiry, preserves `Unknown`, and returns `Idle` without a replacement attempt or fabricated receipt. The effect survives daemon restart with sequence one and the database retains one attempt; the focused proof, strict Clippy, formatting, and diff checks pass. This does not repair the absent receipt or establish full controller-entry/service compatibility.
- Commit `1d62aa9` exercises the actual `OsDeployController::run_osdeploy_once` against the composed IPC adapter with no synthetic `Scenario::ready` admission. When daemon seed facts are absent, the controller returns `Decided(Unknown)` and leaves dispatch, receipt, effect, checkpoint, and native-fake submission empty; the focused PostgreSQL/IPC test, strict Clippy, formatting, and diff checks pass. The positive entrypoint remains blocked by a protocol design gap documented in `fixture-entrypoint-prerequisites.md`: exact request authorization is currently startup-seeded before collection can construct the request, so a late supervisor-controlled authorization phase is required rather than weakening digest binding.
- Commit `23ef0a2` adds the typed late-authorization candidate contract and six rejection/round-trip tests. It separates stable read identity from the future request digest and validates exact committed request, digest, identity, and entered checkpoint owner/generation without granting mutation capability. Strict Clippy and formatting pass. This remains design-only: daemon command routing, durable authorization/release, read-schema migration, and a positive `run_osdeploy_once` path are not implemented; the required protocol is specified in `fixture-late-authorization-protocol.md`.
- Commit `bed4c30` implements the supervisor-controlled runtime seam for late authorization: stable identity is armed, the exact committed request is validated, authorization and checkpoint release are persisted atomically before acknowledgement, and worker/standalone release, mismatch, duplicate, expiry, persistence-failure, and restart cases fail closed. Feature/default daemon checks, strict Clippy, formatting, and diff checks pass. This does not yet enable mutation: versioned read migration, Clone admission integration, and a positive `run_osdeploy_once` proof remain required.
- Commit `b8e861d` implements the versioned stable-read side of that protocol. V2 collection uses a strict identity/timestamp seed without a future request digest; the late adapter is read-only, v1 exact-digest behavior is preserved, and cross-version payloads are rejected. Feature/default checks, focused contracts, strict Clippy, and formatting pass. A separate bound late mutation command still must verify and consume durable authorization before release, so the positive controller entrypoint remains unproven.
- Commit `afc02c2` implements that bound late mutation command. `clone_late` verifies exact request/digest and checkpoint binding, requires released generation/owner/operation, consumes authorization durably before effect creation, and rejects stale, duplicate, mismatched, legacy, restart-invalidated, and persistence-failure cases. Feature daemon/contract tests, strict Clippy, formatting, and diff checks pass. This does not yet wire the full `run_osdeploy_once` collection/admission path or PostgreSQL controller integration.
- Commit `99079a8` exercises a positive fresh `OsDeployController::run_osdeploy_once` Clone flow over v2 IPC and PostgreSQL: controller-owned preflight, attempt/request/dispatch creation, entered checkpoint, mismatch refusal, correct late authorization, one bound submission, exact receipt reload, duplicate refusal, and zero native-fake mutation. The focused test, strict Clippy, formatting, and diff checks pass. It ends `Decided(Unknown)` because fresh post-dispatch task/target observations are unavailable in the static fixture; successful Clone outcome/reconciliation and the complete service loop remain unproven.
- Commit `4c0909a` adds a typed post-dispatch observation contract bound to the exact Clone request, accepted receipt/UPID, identities, and independently supplied acceptance/collection times. Four focused tests, strict Clippy, formatting, and diff checks pass; absence, errors, running, and partial coverage remain explicit and cannot imply success. Durable supervisor publication, read-only lookup, adapter switching, and a positive `Decided(Satisfied)` outcome remain unimplemented.
- Commit `1e99eb2` implements supervisor-owned publication/readback for that contract. It requires exact accepted effect/receipt, atomically persists the bound observation before acknowledgement, rejects unauthorized/stale/wrong-UPID/duplicate publication, and invalidates publication on restart. Five post-dispatch tests, seven Clone-contract tests, strict Clippy, formatting, and diff checks pass. `FixtureProvisioningPort` switching and a positive `Decided(Satisfied)` controller outcome remain unproven.
- Commit `dceb98a` proves a positive fresh `OsDeployController::run_osdeploy_once` Clone outcome. The bounded test-only PostgreSQL journal barrier exposes receipt insertion after the daemon effect is accepted; supervisor publication supplies exact effect/UPID and fresh post-dispatch observations before release, and the controller returns `Decided(Satisfied)`. Three fresh-controller cases pass, including missing-publication `Unknown`, with exact one-attempt/sequence, receipt equality, mismatch/duplicate refusal, strict Clippy, formatting, and diff checks. This remains a Clone-stage proof; sixteen-stage service/callback compatibility, production ingress, writer quiescence, and artifact/rollback acceptance remain open.
- Commit `b41a39e` adds a strict typed first-three-stage message seam: Clone, EnsureCapacity/DiskCapacity, and ConfigurePe are the only admitted stages, with exact request/receipt binding and stage-specific task semantics; later stages and malformed/foreign/cross-stage/zero-sequence messages fail closed. The focused contract, strict Clippy, and formatting pass. Existing fixture daemon/adapter/controller paths remain concrete-Clone only, so coordinated three-stage progression and full service compatibility are not yet proven.
- Commit `f684c46` adds a fail-closed typed `stage_late` transport. It rejects all three stage requests before authority consumption or durable writes, including across daemon restart, while preserving legacy Clone behavior. The stage/Clone contracts, strict Clippy, formatting, and diff checks pass. Progression remains blocked by operation-UUID-only durable deduplication and Clone-only authority/effect/publication paths; `fixture-stage-transport-gate.md` records the required coordinated identity migration. No stage success is claimed.
- Commit `c53e04c` provides the additive stage-bound ledger/replay foundation: v2 records carry stage/attempt/generation/owner, duplicate protection is operation+stage scoped, exact authority and effect rebinding fails closed, and legacy v1 Clone records cannot mix with stage-scoped records. Ten ledger, two stage, and seven Clone-contract tests plus strict Clippy/formatting/diff checks pass. Remaining daemon, checkpoint, effect, publication, and adapter consumers still reject stage transport; no DiskCapacity or ConfigurePe execution is claimed.
- Commit `1ac304c` adds stage-bound checkpoint/accepted-effect consumers and a bound stage mutation client, all behind the existing mutation gate. Exact stage identity and committed bytes are required; restart invalidates authorization and rejected mutation writes no attempt/effect. Serial stage/checkpoint/daemon tests and strict checks pass. One parallel daemon run exposed an unresolved 20 ms ArmLate timing failure before Enter, so concurrency qualification is incomplete. Stage publication/readback and controller adapters remain Clone-only; no DiskCapacity or ConfigurePe execution is claimed.
- Commit `6c2bf9d` repairs that test-only concurrency qualification: ArmLate→Enter now has a one-second scheduling allowance, followed by an explicit post-expiry rejection check; production policies are untouched. Three independent 16-thread daemon runs passed 18 tests each, with strict Clippy, formatting, and diff checks. The timing issue is no longer reproduced, but stage publication/readback and controller adapters remain Clone-only and no DiskCapacity/ConfigurePe execution is claimed.
- Commit `5f66720` proves daemon-side v2 Clone→DiskCapacity acceptance with exact durable predecessor validation. After Clone and daemon restart, resize checks the accepted Clone request/identity/plan and current VM state, consumes stage authorization before its attempt/effect, emits `qmclone`/`resize` receipts, and preserves one attempt per stage; missing or mismatched predecessor evidence fails closed. Four focused stage/consumer tests, strict Clippy, formatting, and diff checks pass. Post-dispatch publication/adapter switching, resize-after-acceptance restart recovery, ConfigurePe, and full controller DiskCapacity success remain unproven.
- Commit `6620757` proves daemon/client restart after DiskCapacity acceptance: fresh connections reload exact Clone and resize effects, preserve two stage attempts/effects, reject duplicate and all identity/digest rebinding, and reject target capacity rebound or malformed resize receipt data. Two consumer tests, strict Clippy, formatting, and diff checks pass. The restart occurs within one integration-test process and does not establish independent OS-worker recovery or ledger-corruption repair; publication/adapter/controller integration and ConfigurePe remain open.
- Commit `8c0a1c5` proves daemon-side synchronous ConfigurePe after exact durable Clone→DiskCapacity predecessors. It validates predecessor identity/receipt, plan/Clone binding, current capacity, and PE configuration before consuming authorization; task receipts and later stages fail closed. The three-stage chain restarts between stages and after ConfigurePe, preserving one attempt/effect per stage and rejecting duplicates, missing/rebound predecessors, and invalid generations. Stage/consumer tests, strict Clippy, formatting, and diff checks pass. Synthetic state remains limited to capacity and PE-configured flags; complete publication, controller progression, and service execution are not proven.
- Commit `b994bde` adds request-aware controller checkpointing: the exact prepared durable request crosses the dispatch/checkpoint boundary, default implementations reject, NativeFakePve preserves its barrier, and the legacy IPC adapter accepts only identity-matching Clone. Feature/default controller and stage tests, strict Clippy, formatting, and diff checks pass. Stage-aware adapter/publication and target/driver readback are still required before claiming controller progression beyond Clone; no three-stage controller success is claimed.
- Commit `8707163` fixes the reproduced macOS parallel framing flake by normalizing accepted Unix sockets to blocking mode before applying existing bounded deadlines. Thirty consecutive 16-thread runs and updated serial/parallel feature plus default parallel suites pass. This is a transport reliability qualification only; Linux and controller/process recovery gates remain open.
- Astra’s review confirms this is a multi-layer refactor (controller ownership/constructor, collection/send helpers, postgres-store permit, and checkpoint), and the minimal fixture world lacks identity/preflight facts for a truthful typed Clone receipt. No partial adapter was committed; the required seam is recorded in `durable-fake-pve-ipc-recovery-design.md`.
- Independent-process recovery is explicitly unproven. Existing Clone → DiskCapacity → ConfigurePe and native restart tests reconstruct controller objects in one process; `NativeFakePve` is in-memory, so a true worker-death/takeover proof requires a separately approved durable deterministic fake-PVE service or IPC fixture.

## Not yet proven

### PeComplete input authority checked at `70ebd0fc` (historical gap; fixture slice closed at `2d82f114`)

The approved boot-files-staged milestone is named in
`docs/superpowers/specs/2026-09-05-rust-osdeploy-durability/next-durable-transaction-boundaries.md:89`,
but its immutable result definition is not implemented. In particular,
`crates/postgres-store/src/osdeploy/package_semantics.rs:69-77` produces only the
registered identity and plan, while
`crates/osdeploy-adapter/src/pe_complete_boundary.rs:124-149` requires the exact
required step identities to come from durable deployment history. The current
plan has no required WinPE step set or boot-files milestone payload schema;
the only callers of that boundary are tests supplying their own IDs.
Authentication of the newly accepted PeRegister result supplies session provenance,
not the missing expected completion evidence at that historical checkpoint.
Consequently a signed `boot_files_staged=true` callback could not yet select
PeComplete success there.

The next schema must bind a versioned, server-admitted completion requirement to
the immutable package: stable required step/milestone IDs, required result fields
and success/failure semantics, plus the canonical definition digest. Delivery
must expose that same definition under the existing session/package binding;
callback adjudication must resolve it from committed state, retain the original
PeCompletion deadline, and select equivalent versus conflicting reports durably.
This is a completion-evidence prerequisite independent of the later generic
guest-action API. Once present, the selected result transaction must also create
the real grace attempt in Waiting with its original scope and due proof, as
specified by `next-durable-schema-proposal.md:49-51`; a grace-only placeholder
would not close this prerequisite. This source audit enabled no accepting API
and claims no new runtime tests; it is retained as the historical reason for
the schema work below.

Subsequent source progress: the `fixture-ipc` feature now exposes explicit
`materialize_fixture_completion_package()`. Its separate canonical package
schema includes the registered plan/identity and a definition digest binding
the package semantic identity, PeComplete operation, and stable
`boot-files-staged.v1` milestone. The fixture result definition requires boolean
`image_applied`, `boot_files_staged`, and `boot_files_verified`; all must be true,
false reports failure, and absent or extra fields must be rejected. This defines
reported fixture evidence, not independent verification of guest disk contents.
Focused tests verify determinism, exact canonical digest, changed operation
bindings, rejection of a changed unregistered plan fingerprint, and preservation
of the existing materialization bytes. Delivery now uses the persisted
completion-schema selector for the fixture entry point. Commit `2d82f114`
validates the selected package and report, persists the fixture-only completion
result, and creates/repairs a parked grace projection. No callback acceptance is
enabled for legacy or generic action/result surfaces by this package work.

Delivery adoption now has an explicit immutable origin selector. Migration 0013
defaults existing origins to their original package schema; only the new
`create_fixture_osdeploy_with_completion_package()` entry point selects the
completion schema and requires a non-nil private sink. Replaying a create key
with the other schema conflicts. Session arming, alias issuance, and execution
reload all resolve bytes from this persisted selector. This preserves historical
session/alias digests and binds newly opted-in delivery to the completion
definition. Callback report adjudication and grace activation remain closed.
Verification: both completion-package integration tests passed (77.17 seconds),
covering delivery/reclaim/reload, exact schema/digest, concurrent origin creation,
schema replay conflict, immutable selector, and repeated migration. Historical
alias replay/renew/rollback/reopen passed (10.63 seconds); all-feature store
library/test Clippy with warnings denied, no-feature compilation, formatting and
whitespace checks passed.

- Complete sixteen-stage OSDeploy service execution, including callback, guest-agent, host-side QGA, media, firmware, disk-growth, and terminal/recovery behavior.
- Independent-process restart, crash, restore, rollback, and response-loss behavior across the complete service workflow.
- Compatibility with every retained Python/Ansible ingress and callback contract under a real dual-executor handoff.
- Actual quiescence and generation fencing of the current Python/Ansible writer before Rust becomes the sole mutation writer.
- Backup/restore rehearsal, immutable release artifact/export verification, operator acceptance, rollback window, and non-production sacrificial workflow proof.
- OOBE, ESP, enrollment, usable-device, and production operational acceptance. These remain separate from controller unit and fixture tests.

## Authorization gates

The owned-v1 Linux runner was executed under the user's explicit local-only approval with the reviewed resource and observation profile (4 GiB PostgreSQL tmpfs, 512 MiB reserve, 6 GiB PostgreSQL cap, 4 GiB runner cap, private cgroup namespace, no swap, and the stated smoke/final-catalog rules). That approval does not extend to production mutation or deployment.

Production access, deployment, mutation, Ansible retirement, and cutover each require separate approval. No evidence in this assessment grants those permissions.

## Readiness level

Current level: **local PoC / release-candidate preparation**.  
Target level: **production candidate pending external gates**.  
Decision owner: Adam, after the remaining service, independent-process handoff/recovery, artifact/rollback, non-production, and operator-acceptance evidence is complete.

## Evidence index

- [macOS regression receipt](../../restart-task9-macos-full-2.receipt.json)
- [Linux build receipt](../../restart-task9-linux-build-5.receipt.json)
- [Linux Compose output](../../restart-task9-linux-compose-1.stdout.log)
- [Python contract receipt](../../restart-task9-python-contracts-3.receipt.json)
- [Readiness tracker](poc-readiness-tracker.md)
- [Owned Linux full-run evidence](restart-task9-owned-full-4/)
- [Changed-source owned Linux full-run evidence](restart-task9-owned-full-guest-1/)
- [Restart/recovery and compatibility evidence](restart-recovery-1/acceptance.md)
