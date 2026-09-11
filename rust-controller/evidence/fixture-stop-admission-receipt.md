# Immutable supervisor stop admission receipt

The fixture supervisor returns `FixtureStopAdmissionReceiptV1` only after
`FixtureLog::admit_stop` persists and syncs the validated admission. The receipt
contains the exact admission record: stop operation/request/binding, guarded
grace authority, immutable StartPe predecessor, selected power observation and
original admission clock. Its SHA-256 covers that complete record. Exact replay
returns the original record and digest, including after ledger reload.

`StageCheckpointReply.stop_admission` is response-specific. Refusal and status
responses carry no admission receipt. `OsDeployController::admit_fixture_stop`
requires a receipt on a successful supervisor response and verifies its selected
power scope against the versioned sample. This binds sample digest/sequence,
stop identity, authority, StartPe identity, VM, daemon generation and observation
clock. It returns that receipt to its caller for the later durable outbox bridge.

The receipt is evidence from the trusted local supervisor transport, not a
cryptographic capability. Decoding arbitrary receipt JSON does not establish
provenance. No PostgreSQL exposure decision, outbox consumption, barrier release,
stop attempt/effect, or Stopped-power observation is implemented by this change.
Cancellation and lease checks remain point-in-time checks before supervisor
admission; the receipt must not bypass the future durable send decision.

## Verification

- Focused ledger proof passed (1 test): completed StartPe prerequisite,
  scoped Running-power receipt, serialization round trip, digest stability,
  exact replay/reload, changed sample sequence/digest, owner, authority,
  daemon generation and observation-clock refusal. Existing torn ledger and
  stale ownership checks remain in the same test.
- Independent daemon/worker restart refusal test passed (1 test, 2.14 seconds):
  unproven admissions and subsequent status responses expose no receipt; the
  barrier remains parked and physical attempts/effects remain absent.
- Workspace all-feature compilation passed. All-target/all-feature Clippy for
  `pve-port` and `operation-controller` passed with warnings denied.

These tests prove the ledger receipt and refusal transport boundaries. A full
positive PostgreSQL-to-supervisor-to-outbox workflow remains to be implemented
and verified, along with ambiguous acknowledgement recovery and stop execution.
