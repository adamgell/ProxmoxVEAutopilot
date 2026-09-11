# Atomic StartPe fixture IPC submission

This connects the typed power-aware release from `a908521` to the atomic fixture
ledger admission seam. No real `qmstart`, Proxmox request, production/default-path
change or controller satisfaction is enabled.

At submission, the barrier requires the exact released request/identity and a
power-aware token. It revalidates that token against the current daemon's latest
durable stopped record and the five-second freshness bound. The typed StartPe
submission then revalidates the exact accepted ConfigurePe request/receipt, token,
VM and unchanged disk/PE state before invoking the atomic seam. The seam consumes
live authority before persisting one frame containing the attempt, exact synthetic
`qmstart` receipt and stopped-to-running transition. Generic disk-only release
still cannot admit StartPe. Rejected tokens do not create attempts or effects.

The synthetic receipt means fixture admission only. It creates no power
observation, completed task result, or successful controller outcome. StartPe
post-dispatch publication is explicitly refused, including a structurally valid
task/running observation document, until its durable independent evidence
contract is implemented. Existing adapter/controller StartPe gates remain closed.

## Proof

The fresh real-daemon Clone→resize→ConfigurePe chain now obtains exact power-aware
authorization and accepts one StartPe transition. The test verifies the target
`qmstart` receipt, exactly one appended atomic frame, four attempts/effects,
consumed authority, and no added running observation. Duplicate submission leaves
the ledger byte-identical. Daemon restart recovers the exact accepted effect and
receipt; neither old power evidence nor a new generic release replays the effect.
A separate daemon rejects a truncated atomic frame copied from this actual IPC
acceptance, rather than exposing partial acceptance.

Ledger tests exercise submission-token freshness boundaries (including stale and
future time), altered-token rejection and exact token replay, in addition to the
existing atomic acceptance/receipt/torn-frame proofs. The publication test first
checks that its adversarial observation document passes structural decoding, then
proves that supervisor publication still rejects it. This is not a live running
observation or a controller-ready claim.

Validation passed: two fixture-feature ledger tests, two default-feature ledger
tests, 15 fixture stage/post-dispatch/consumer tests, strict all-target Clippy for
`pve-port` and `operation-controller`, formatting and diff checks.

Remaining: independently collected durable task/running observation publication,
explicit adapter restoration, a fresh PostgreSQL-backed four-stage controller
satisfaction proof, and OS-worker process-death recovery windows. The torn-record
test is not an OS-worker kill proof. Callbacks and later stages remain separate.
